# INS-C2-006 — Test Specification

Every case below maps to a test that ships in this repository. The suite runs
without a platform connection:

```bash
python -m pytest tests/ -v
```

| File | Scope |
|---|---|
| `tests/unit/test_agent.py` | Node behaviour, the rule set, and the graph |
| `tests/unit/test_framework_compliance_tc06_tc07.py` | The framework's gate methods are not overridable |
| `tests/proof_of_boundary/test_pb_invoke_endpoint.py` | End to end through the real HTTP entry point |
| `tests/proof_of_boundary/test_pb_invoke_order.py` | Backbone execution order and the trust gate |
| `tests/proof_of_boundary/test_state_safety.py` | State carries only checkpoint-safe types |
| `tests/proof_of_boundary/test_import_isolation.py` | The template imports nothing beneath the framework |
| `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` | Documented skip: this agent has no human-in-the-loop step |

## 1. Unit tests — `tests/unit/test_agent.py`

### TC-U-01: PreProcessNode — payload screening (`TestPreProcessNode`)

Refusals split by whether the caller can repair the request. A correctable one
completes (`SUCCESS`) carrying a marker; an unfixable one terminates (`ERROR`).
The cases below assert the status each class is contracted to produce, so a
regression that collapsed the two would fail here.

| ID | Case | Expected |
|----|------|----------|
| U-01-01 | Empty `user_input` | SUCCESS — correctable, error_log populated |
| U-01-02 | Whitespace-only input | SUCCESS — correctable |
| U-01-03 | Non-string input | SUCCESS — correctable |
| U-01-04 | Input past the 64 KB ceiling | SUCCESS — correctable, nothing validated |
| U-01-05 | Payload reduced to the mask sentinel | ERROR — no application content survives to evaluate |
| U-01-06 | Valid application JSON | SUCCESS, `input_record` + `validated_input` set, status is a `str` |
| U-01-07 | `required_trust_level` declaration | VERIFIED_EXTERNAL |

### TC-U-02: Injection refusal (`TestInjectionRefusal`)

The refusal is asserted through `execute()` called directly — with no wrapper in
front, nothing else can be doing the work.

| ID | Case | Expected |
|----|------|----------|
| U-02-01 | Six injection forms, parametrized | ERROR — terminating, `validated_input` absent |
| U-02-02 | Three ordinary sentences containing similar words | SUCCESS — no false refusal |

### TC-U-03: Trust gate (`TestTrustGate`)

Invocations route through `BaseNode.__call__`, so the gate runs before `execute()`.

| ID | Case | Expected |
|----|------|----------|
| U-03-01 | ANONYMOUS caller | ERROR, "trust gate denied", `execute()` never ran |
| U-03-02 | VERIFIED_EXTERNAL caller | SUCCESS |

### TC-U-04: Caller-metadata contract (`TestCallerMetadataContract`)

| ID | Case | Expected |
|----|------|----------|
| U-04-01 | No metadata supplied | SUCCESS on documented defaults |
| U-04-02 | All four fields valid | SUCCESS, values carried into State |
| U-04-03 | Each identifier field × 8 non-inert values (prose, markup, over-length, empty, int, bool, None, list) | SUCCESS — correctable; `error_log` names the field |
| U-04-04 | Rejected value carries a marker string | Marker absent from the whole response |
| U-04-05 | Unknown metadata key | SUCCESS — correctable; refused, not ignored |
| U-04-06 | Unknown profile name | SUCCESS — correctable; `error_log` names `rule_profile` |
| U-04-07 | `input_context` is not a mapping | SUCCESS — correctable |

### TC-U-05: The rule set (`TestRuleSet`)

| ID | Case | Expected |
|----|------|----------|
| U-05-01 | The shipped `config/rules.yaml` | Loads; `standard` present with the documented floor |
| U-05-02 | Profile selection with and without a name | Named profile, else the default |
| U-05-03 | Rule file absent | The built-in baseline applies |
| U-05-04 | Threshold × the 9-value non-finite matrix | `RuleSetError` |
| U-05-05 | Coverage band out of order | `RuleSetError` |
| U-05-06 | Age window out of order | `RuleSetError` |
| U-05-07 | Threshold key missing | `RuleSetError` |
| U-05-08 | `default_profile` names no declared profile | `RuleSetError` |
| U-05-09 | Non-inert policy-type entry | `RuleSetError` |

### TC-U-06: ParseNode (`TestParseNode`)

| ID | Case | Expected |
|----|------|----------|
| U-06-01 | Valid JSON application | SUCCESS, `parsed_field_count == 5` |
| U-06-02 | Form-encoded application | SUCCESS, `parsed_field_count == 3` |
| U-06-03 | Empty `input_record` | ERROR — the upstream boundary guarantees this field; its absence is not a caller error |
| U-06-04 | Malformed JSON | SUCCESS — correctable |
| U-06-05 | Result inspected for raw values | No field value present |

### TC-U-07: UnderwritingRuleValidateNode (`TestUnderwritingRuleValidateNode`)

| ID | Case | Expected |
|----|------|----------|
| U-07-01 | Complete valid application | Every field `pass` |
| U-07-02 | Required fields missing | `fail` with reason `missing` |
| U-07-03 | Age outside the window | `fail` / `out_of_window` |
| U-07-04 | Coverage below the floor | `fail` / `below_minimum` |
| U-07-05 | Coverage above the ceiling | `fail` / `above_ceiling` |
| U-07-06 | Coverage at or above the review mark | `warn` / `above_threshold` |
| U-07-07 | Unsupported policy type | `fail` / `unsupported_value` |
| U-07-08 | Malformed occupation code | `warn` / `format_mismatch` |
| U-07-09 | Term and beneficiary count past their limits | `warn` |
| U-07-10 | 4 numeric fields × the 9-value non-finite matrix (36 cases) | `fail` / `not_finite` — never a pass |
| U-07-11 | Same application under two profiles | Different outcomes per profile |
| U-07-12 | Result inspected for raw values | No field value present |
| U-07-13 | `input_record` missing | ERROR |

### TC-U-08: RiskFlagNode (`TestRiskFlagNode`)

One case per flag in the taxonomy, driven by the reason codes:
no flags on all-pass, INCOMPLETE_APPLICATION, ELIGIBILITY_FAIL,
COVERAGE_BELOW_MINIMUM, COVERAGE_ABOVE_CEILING, HIGH_RISK_INDICATOR,
INVALID_POLICY_TYPE, INVALID_NUMERIC, FORMAT_ERROR, an unmapped reason still
surfacing a flag, and ERROR when `validation_results` is absent.

### TC-U-09: GenerateReportNode (`TestGenerateReportNode`)

| ID | Case | Expected |
|----|------|----------|
| U-09-01 | All pass | `PASS`, no remediation |
| U-09-02 | A warn result | `NIGO` with remediation |
| U-09-03 | ELIGIBILITY_FAIL present | `REJECT` |
| U-09-04 | COVERAGE_ABOVE_CEILING present | `REJECT` |
| U-09-05 | Report structure | Title, reference, profile, both sections, footer |
| U-09-06 | No application reference supplied | `(not supplied)` placeholder |
| U-09-07 | Remediation text under two profiles | Figures track the active profile |
| U-09-08 | `validation_results` missing | ERROR |

### TC-U-10: The output boundary (`TestOutputBoundary`)

Both directions: an authorised report passes untouched, and every leak form is
refused by the layer that owns it.

| ID | Case | Expected |
|----|------|----------|
| U-10-01 | Authorised report | Passes, no layer fires |
| U-10-02 | Node emits it | SUCCESS, `formatted_output` byte-identical, no undeclared `output` key |
| U-10-03 | Empty report | Refused by `structure` |
| U-10-04 | Footer removed | Refused by `structure` |
| U-10-05 | Report past the size ceiling | Refused by `structure` |
| U-10-06 | 7 credential/personal-data forms | Refused by `pattern_scan` — asserted to be the deciding layer, proving the scan runs before anything rewrites the text |
| U-10-07 | Submitted application appended verbatim | Refused by `verbatim_scan` |
| U-10-08 | Short incidental overlap ("PASS") | Passes — not treated as a leak |
| U-10-09 | 6 unauthorised line forms (applicant name, an amount, an invented label, an invented flag, free-text remediation, prose) | Refused by `vocabulary` |
| U-10-10 | 3 non-inert identifier lines | Refused by `vocabulary` |
| U-10-11 | A remediation line belonging to another profile | Refused under `standard`, accepted under `senior_simplified` |
| U-10-12 | `report_output` missing | ERROR |

### TC-U-11: Graph end to end (`TestGraphEndToEnd`)

| ID | Case | Expected |
|----|------|----------|
| U-11-01 | Valid application | SUCCESS, footer present, `Overall Status: PASS` |
| U-11-02 | Required fields missing | SUCCESS, `Overall Status: NIGO` |
| U-11-03 | Bare `NaN` coverage amount in the payload | Never `Overall Status: PASS` |
| U-11-04 | Caller metadata supplied | Reference and profile rendered in the report |
| U-11-05 | ANONYMOUS caller | Not SUCCESS |
| U-11-06 | Injection payload | Not SUCCESS, nothing published |
| U-11-07 | Runtime config | `max_retry` / `timeout_s` reach `agent.config` |
| U-11-08 | 4 malformed runtime configs | `ConfigError` at compile |

## 2. Framework compliance — `tests/unit/test_framework_compliance_tc06_tc07.py`

| ID | Case | Expected |
|----|------|----------|
| TC-06 | A node subclass overrides the default input gate | `TypeError` at class definition |
| TC-07 | A node subclass overrides the default output gate | `TypeError` at class definition |

## 3. Boundary tests — `tests/proof_of_boundary/`

### PB-8: the real HTTP entry point (`test_pb_invoke_endpoint.py`)

The full stack — HTTP adapter, Bearer-token trust promotion, runtime config
loading, the compiled graph, and the output boundary — reached the way an
external caller reaches it.

| ID | Case | Expected |
|----|------|----------|
| PB8-01 | `GET /health` | 200, `status: ok` |
| PB8-02 | Runtime config after start-up | `max_retry == 3`, `timeout_s == 30` |
| PB8-03 | Authenticated invoke with metadata | SUCCESS; report reflects THIS application and THIS metadata |
| PB8-04 | Two different applications | Different reports; the second is NIGO with HIGH_RISK_INDICATOR |
| PB8-05 | Same application, two profiles | REJECT under `standard`, NIGO under `senior_simplified` |
| PB8-06 | No credential | ERROR — terminating, nothing published |
| PB8-07 | Wrong credential | ERROR — terminating, nothing published |
| PB8-08 | 7 invalid metadata payloads | SUCCESS — correctable; a body is returned and the marker string never appears in the response |
| PB8-09 | 3 non-finite literals × 3 numeric fields, sent as raw JSON | Never a PASS decision (or a 400/422 at the adapter) |
| PB8-10 | `input_context` past 256 KB | 413 at the adapter |
| PB8-11 | Injection payload | ERROR — terminating, nothing published |
| PB8-12 | Emitted reports, two profiles | Every line inside the authorised vocabulary |
| PB8-13 | Application with distinctive values | None of them appear in the report |

### PB-6: backbone order (`test_pb_invoke_order.py`)

| ID | Case | Expected |
|----|------|----------|
| PB6-01 | Node history on success | The eight backbone nodes in order |
| PB6-02 | ANONYMOUS caller | Denied; no domain node runs |
| PB6-03 | The `main` slot | Holds a FunctionNode |
| PB6-04 | Result mapping | `result["output"]` present |
| PB6-05 | Empty input, end to end | SUCCESS with a body naming what to correct; no domain field produced |

### PB-2 / PB-5: state safety (`test_state_safety.py`)

State declares no Pydantic model, no context object, and no credential-shaped
field name.

### PB-4: import isolation (`test_import_isolation.py`)

No module under `src/` imports beneath the framework.

### PB-7: human-in-the-loop propagation (`test_pb7_hitl_interrupt_propagation.py`)

This agent is a synchronous pipeline with no human-in-the-loop step, so the file
is a documented skip rather than a gap in the suite.

## 4. Coverage of the design contracts

| Contract | Cases |
|---|---|
| Refusal contract — correctable completes | U-01-01 … U-01-04, U-04-03, U-04-05 … U-04-07, U-06-04, PB6-05, PB8-08 |
| Refusal contract — unfixable terminates | U-01-05, U-02-01, U-03-01, U-10-03 … U-10-12, U-11-05, U-11-06, PB8-06, PB8-07, PB8-11 |
| Trust boundary | U-01-07, U-03-01/02, PB6-02, PB8-06, PB8-07 |
| Payload screening | U-01-01 … U-01-06, U-02-01/02, PB8-11 |
| Caller-metadata contract | U-04-01 … U-04-07, PB8-08, PB8-10 |
| Finite/bounded numerics | U-05-04, U-07-10, U-11-03, U-11-08, PB8-09 |
| Live rule configuration | U-05-01 … U-05-09, U-07-11, U-09-07, PB8-05 |
| Output invariant | U-10-01 … U-10-12, PB8-12, PB8-13 |
| Audit | `emit_trace_event` patched per node module; every path exercised |
| Credentials | No external call and no secret is required; `requires` is empty in the manifest |
