# INS-C2-006 — Design Specification

## Base Design

- **Agent Class**: `InsC2006Agent` (inherits `AgentBaseGraph` directly)
- **Architecture**: flat Cat-2 — a six-node linear domain pipeline, no inner graph
- **Pattern**: deterministic rule pipeline — validated application record → structured report
- **State**: flat TypedDict (`State(AgentState)`) — no Pydantic, msgpack-compatible
- **Generation mode**: deterministic — the agent makes no model call

| Layer | Component |
|---|---|
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |

## Architecture Overview

### Pipeline

```
START → initialize → pre_process → parse (main) → uw_validate → risk_flag
      → generate_report → post_process → finalize → END

Terminating short-circuit (after pre_process):
  the run is abandoned → finalize (the domain pipeline never runs)
```

The conditional edge at `pre_process` tests the status only. A refusal the
caller can correct keeps the run in a completed state, so it stays on the main
path: every domain node ahead of it reads the refusal marker first and returns
it untouched, and `PostProcessNode` renders the reason as the caller-facing
body. No domain field is computed on either route — the difference is which
edge the run takes, not whether the application is evaluated. See *The Refusal
Contract* below.

Because this is a flat Cat-2, the caller's `input_context` reaches every node
through the shared state. There is no `GraphNode` in the pipeline and therefore
no bridging step.

### Node Composition

| Slot | Class | Responsibility | Trust Level |
|------|-------|---------------|-------------|
| initialize | InitializeNode (framework) | Schema version, session id, trust propagation | ANONYMOUS |
| pre_process | PreProcessNode | Input boundary: trust gate, payload screening, caller-metadata contract | **VERIFIED_EXTERNAL** |
| main | ParseNode | Parse JSON/form-encoded → `parsed_field_count` only | ANONYMOUS |
| uw_validate | UnderwritingRuleValidateNode | Apply the rule set → `validation_results`, `validation_reasons` | ANONYMOUS |
| risk_flag | RiskFlagNode | Map reason codes onto the flag taxonomy → `risk_flags` | ANONYMOUS |
| generate_report | GenerateReportNode | Decide and render → `overall_status`, `remediation_suggestions`, `report_output` | ANONYMOUS |
| post_process | PostProcessNode | Output boundary → `formatted_output` | ANONYMOUS |
| finalize | FinalizeNode (framework) | Response metadata, total time | ANONYMOUS |

### Data Flow

```
user_input (raw JSON / form-encoded application)
input_context (caller metadata)
    │
    ▼ PreProcessNode
    ├── trust gate (VERIFIED_EXTERNAL)
    ├── payload screening: type, empty, size ceiling, injection, mask sentinel
    ├── caller-metadata contract: inert identifiers, known keys, known profile
    └── writes: input_record, validated_input, submission_channel,
                submitter_ref, application_ref, rule_profile
    │
    ▼ ParseNode  (main slot)
    ├── parse input_record locally (JSON first, form-encoded fallback)
    ├── raw field VALUES: local to execute() only — never persisted
    └── writes: parsed_field_count
    │
    ▼ UnderwritingRuleValidateNode
    ├── load the rule set (config/rules.yaml), select the caller's profile
    ├── every application number through the finite+bounded parser
    └── writes: validation_results (field → pass/warn/fail)
                validation_reasons (field → reason code)
    │
    ▼ RiskFlagNode
    └── writes: risk_flags (JSON list, e.g. ["ELIGIBILITY_FAIL"])
    │
    ▼ GenerateReportNode
    ├── decide PASS / NIGO / REJECT
    ├── build remediation sentences from the active profile's thresholds
    └── writes: overall_status, remediation_suggestions, report_output
    │
    ▼ PostProcessNode
    ├── output boundary: structure → patterns → verbatim → vocabulary
    └── writes: formatted_output  ← get_output() maps it to result["output"]
    │
    ▼ FinalizeNode → result["output"] delivered to the caller
```

### State Definition

| Field | Type | Writer | Purpose |
|-------|------|--------|---------|
| input_record | str | PreProcessNode | Raw application JSON/form-encoded |
| validated_input | str (inherited) | PreProcessNode | Canonical audit reference |
| submission_channel | str | PreProcessNode | Validated caller channel (audit only) |
| submitter_ref | str | PreProcessNode | Validated submitter reference (audit only) |
| application_ref | str | PreProcessNode | Validated application reference (rendered) |
| rule_profile | str | PreProcessNode | Selected threshold profile |
| parsed_field_count | int | ParseNode | Count of recognised fields (no raw values) |
| validation_results | str (JSON) | UnderwritingRuleValidateNode | Per-field pass/warn/fail map |
| validation_reasons | str (JSON) | UnderwritingRuleValidateNode | Per-field reason code (internal) |
| risk_flags | str (JSON) | RiskFlagNode | List of risk-flag labels |
| overall_status | str | GenerateReportNode | PASS / NIGO / REJECT |
| remediation_suggestions | str (JSON) | GenerateReportNode | Correction guidance |
| report_output | str | GenerateReportNode | The rendered report |
| formatted_output | Any (inherited) | PostProcessNode | Final output emitted to the caller |
| error_code | str | Any node that declines a correctable request | Refusal marker — `EMPTY_INPUT` / `QUESTION_TOO_LONG` / `INVALID_REQUEST`; internal, never emitted to the caller |
| error_message | str | Refusal paths | Human-readable error detail (internal) |

**State privacy constraints:**
- No individual applicant fields: no `applicant_name`, `date_of_birth`, `address`, `health_condition`
- `input_record` holds the raw application payload — the ONLY raw-input state field
- ParseNode and UnderwritingRuleValidateNode consume raw values locally; none are persisted
- All downstream fields carry derived flags, counts and codes only

## The Caller-Data Contract

`/invoke` accepts three parameters: `input` (the application), `session_id`, and
`input_context` (structured caller metadata). The HTTP adapter caps the
serialized `input_context` at 256 KB; everything else is validated field by
field in PreProcessNode, before any other node may consume it.

| Field | Type | Bound | Default | Effect |
|---|---|---|---|---|
| `channel` | identifier | `[a-z0-9_]{1,32}` | `unknown` | Audit attribution only |
| `submitter_ref` | identifier | `[a-z0-9_]{1,32}` | `""` | Audit attribution only |
| `application_ref` | identifier | `[a-z0-9_]{1,32}` | `""` | Rendered in the report header |
| `rule_profile` | identifier | must name a profile in `config/rules.yaml` | `default_profile` | Selects the threshold set |

Rules that hold for all four:

- **Unknown keys are refused**, not ignored — a typo in a field name fails the
  request rather than silently applying a default.
- **Failure is closed**: the request is not evaluated, the refusal names the
  FIELD and never echoes the value. An invalid metadata field is correctable,
  so the run completes carrying `INVALID_REQUEST` rather than terminating.
- **Absent means default**: the documented default applies, and the pipeline
  still computes a real result from the submitted application.
- **Only inert identifiers**: no caller free text can reach the report.

## The Refusal Contract

Not every refusal is the same kind of event, and the status field is what tells
them apart. Two classes, decided by one question: **can the caller fix this by
sending a different request?**

### Correctable — the run COMPLETES without evaluating the application

`status = AgentStatus.SUCCESS.value`, plus an internal `error_code` marker.

| Trigger | Marker | Node |
|---|---|---|
| `user_input` absent, empty, or whitespace only | `EMPTY_INPUT` | PreProcessNode |
| `user_input` past the 64 KB ceiling | `QUESTION_TOO_LONG` | PreProcessNode |
| `user_input` is not a string | `INVALID_REQUEST` | PreProcessNode |
| `input_context` is not a mapping | `INVALID_REQUEST` | PreProcessNode |
| An `input_context` key outside the contract | `INVALID_REQUEST` | PreProcessNode |
| A metadata identifier that is not inert, or an unknown `rule_profile` | `INVALID_REQUEST` | PreProcessNode |
| The application payload does not parse as JSON or form-encoded | `INVALID_REQUEST` | ParseNode |

Terminating here would end the calling surface's turn and surface an exception
type in place of the reason, leaving the correction reachable only from the
audit trail. Completing instead lets the caller repair the request and send it
again on the same conversation.

What the caller receives is a sentence, not the marker: `error_code` is State
only and is never placed in the response envelope. `PostProcessNode` maps the
marker onto one of the fixed sentences in `src/services/failure_message.py` and
returns it as `formatted_output`, which `get_output()` surfaces as
`result["output"]`. Each sentence names WHAT to correct and nothing else — it
never echoes the rejected value, names an internal field path, or quotes a gate
message. Those stay in `error_log`.

No domain field is written on this route. Every node between the refusal and
the output boundary reads `error_code` first and returns it unchanged, so
`validation_results`, `risk_flags`, `overall_status` and `report_output` are
absent from the result.

### Terminating — the run is ABANDONED

`status = AgentStatus.ERROR.value`, no output, reason in `error_log` only.

| Trigger | Node |
|---|---|
| Caller trust below `VERIFIED_EXTERNAL` | framework trust gate, before PreProcessNode |
| An injection marker in the application payload | PreProcessNode |
| The payload arrived reduced to the mask sentinel — the input gate replaced every detected personal-data span, so no application content is left | PreProcessNode |
| The underwriting rule set is unusable | PreProcessNode |
| A required upstream field is missing where the pipeline guarantees it — no `input_record`, `validation_results`, or `report_output` | ParseNode, UnderwritingRuleValidateNode, RiskFlagNode, GenerateReportNode, PostProcessNode |
| The output boundary refuses the rendered report (any of the four layers) | PostProcessNode |

These are not caller errors in a form the caller can correct by rewording, and
two of them — the injection attempt and an output-gate refusal — must not hand
back a body that hints at what the boundary saw. **They do not degrade to a
completed run.** A terminating refusal ends the invocation; it publishes
nothing.

## Numeric Handling

Every number in a submitted application is caller-controlled and decides an
underwriting outcome, so each goes through `finite_in_range()`
(`src/schemas/state.py`) rather than a bare `float()` / `int()`:

- `bool` is refused — it is an `int` subclass, so `True` would otherwise parse as 1;
- only plain decimal text parses — no exponent notation, no `nan`/`inf` spellings;
- NaN and ±Infinity are refused explicitly: they survive `float()`, and every
  ordered comparison against them evaluates False, so an unchecked NaN coverage
  amount would clear every threshold and be reported as acceptable;
- values outside the field's outer bound are refused.

A refused value fails CLOSED: the field is marked `fail` with reason
`not_finite`, which raises `INVALID_NUMERIC` and prevents a PASS decision.

The same parser validates the declared runtime parameters (`max_retry`,
`timeout_s` in `config/config.yaml`, checked in `_validate_config()` at compile
time) and every threshold in `config/rules.yaml` at load time.

## The Output Boundary

The agent's external-output invariant is a privacy one:

> the validation report carries field NAMES, result LABELS, risk-flag labels,
> remediation sentences derived from the configured thresholds, and the
> caller's inert application reference — and nothing else.

**No rounding grid applies here.** The report renders no caller-derived monetary
aggregate: the only figures in it are the configured underwriting thresholds,
and the vocabulary check below pins those to the active profile, so a threshold
figure cannot drift either. The invariant to enforce is the vocabulary itself,
and it is enforced for every line.

`PostProcessNode` applies four independent layers, in order, each with its own
audit event, each failing CLOSED (the report is withheld and the node returns
ERROR):

| # | Layer | Refuses |
|---|---|---|
| 1 | structure | Empty report, missing mandatory footer, output past the 50,000-char ceiling |
| 2 | pattern scan | Credential and personal-data forms (API key, token, government id, payment card, email, date of birth, health/birth field markers) |
| 3 | verbatim scan | Any substantial span of the submitted application appearing in the report |
| 4 | vocabulary | Any line outside the set the report contract authorises for the active profile |

Layer order is deliberate. The pattern scan runs FIRST among the content checks
and on the untouched report text: a scan placed after any step that rewrites the
text can miss a pattern whose characters were altered in between.

Layer 4 is what makes the invariant complete. `PostProcessNode` rebuilds the
permitted line set from the rule set — not from the generator's output — so a
generator change that started rendering applicant data produces lines the
contract never authorised, and the report is withheld. Identifier-bearing lines
(the application reference, the profile name) are re-checked against the inert
pattern at the boundary.

## Framework Utilisation

- Trust gate: `required_trust_level = TrustLevel.VERIFIED_EXTERNAL` on PreProcessNode;
  inner domain nodes declare ANONYMOUS because the boundary is enforced once, at entry.
- Input screening: the injection scan lives inside `PreProcessNode.execute()`, so
  the refusal holds when `execute()` is called directly, not only behind the
  framework's own input gate.
- Output gate: module-level `_security_gate_output()` in `post_process_node.py`,
  called from `execute()`. It is not an instance method — the framework marks its
  own gate methods final and auto-wraps node instance methods on the invoke path.
- Audit: `emit_trace_event(event, payload, state)` (positional) in every node.
  Domain events only — `node_start` / `node_complete` / `node_error` are emitted
  by the framework base node.
- Credentials: the agent makes no external call and requires no secret;
  `requires.secrets` and `requires.extras` are both empty in the manifest.

## Configuration

| File | Contents |
|---|---|
| `config/agent.yaml` | The manifest, flat at root level: `id`, `name`, `namespace`, `class` (single dotted path), `category`, `industry`, `generation_mode`, `required_trust_level`, `requires.secrets`, `requires.extras` |
| `config/config.yaml` | Runtime parameters passed to the graph constructor: `max_retry`, `timeout_s` |
| `config/rules.yaml` | The underwriting rule set, loaded at runtime by `src/services/service.py` |

`config/rules.yaml` is live configuration, not documentation: the code holds no
second copy of the thresholds. Editing the file changes agent behaviour, and
every value is re-validated on load (finite, in range, correctly ordered). A
malformed value fails the request closed; an absent file falls back to the
built-in baseline in `src/services/service.py`.

## Import Isolation

- The template does not import the platform SDK underneath the framework
- Import targets: `framework.*`, `shared.*`, `src.*` only
- No imports from other agent templates

## Design Decisions

| Decision | Chosen | Rationale |
|----------|--------|-----------|
| Base class | AgentBaseGraph | Standard flat pipeline; no autonomous loop needed |
| Composition | Flat (six-node linear) | The job is sequential; an inner graph would add a bridging problem for no gain |
| Raw applicant data in State | Only `input_record` | Everything downstream is a derived flag, count or code |
| Output gate placement | Module-level helper | The framework marks its gate methods final and auto-wraps instance methods |
| Output invariant | Closed vocabulary, not a rounding grid | The report renders no caller-derived monetary aggregate |
| Rule storage | `config/rules.yaml`, loaded at runtime | A configuration file the code duplicates is a file that silently goes stale |
| Refusal classification | Correctable completes; unfixable terminates | A value the caller can repair should end the run with the reason in the body, not end the caller's turn with an exception type |
| Refusal carrier | `error_code` in State, sentence in the body | The marker drives routing and node short-circuits; the caller gets a sentence naming what to correct, never an internal code |
| Refused-input routing | Conditional at pre_process on status only | A terminating refusal skips the domain pipeline; a correctable one stays on the path and is carried through by per-node marker checks, which computes no domain field either way |
