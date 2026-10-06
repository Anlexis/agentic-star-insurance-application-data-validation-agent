"""AgentCore Platform v1.0 — INS-C2-006 State schema."""

# The framework package ships without a py.typed marker, so AgentState resolves
# to Any for a type checker and this class is not recognised as a TypedDict —
# which makes NotRequired[] unverifiable HERE. At runtime AgentState is a
# genuine TypedDict and these fields must stay NotRequired (they are written by
# nodes, not present at graph initialisation), so the annotations are kept and
# only that one check is disabled.
# mypy: disable-error-code="valid-type"

# State is a flat TypedDict — never a Pydantic BaseModel.  LangGraph checkpoints
# use msgpack serialization; Pydantic objects cause silent corruption.  Extend
# AgentState with agent-specific fields only.  Do NOT add credentials, secrets,
# or Pydantic objects.
#
# Privacy contract (INS-C2-006):
#   NO raw customer PII fields (applicant name, date of birth, address, health
#   data) are stored in State.  Only DERIVED results are persisted:
#   parsed_field_count, validation_results (pass/warn/fail per field NAME),
#   risk_flags, overall_status, remediation_suggestions, report_output.
#   ParseNode and UnderwritingRuleValidateNode consume raw values locally
#   within execute() — they are never written to state.
#
# This module also holds the two caller-input parsers every node shares:
# finite_in_range() for numbers and is_inert_identifier() for strings.

import json
import math
import re
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState

# A caller-supplied string that is allowed to reach the report must be an inert
# identifier: lowercase alphanumerics and underscores, at most 32 characters.
# Free text on such a field would be caller-controlled content rendered into an
# external document.
_INERT_IDENTIFIER_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# Accepted textual number forms.  Deliberately narrow: no exponent notation, no
# "nan"/"inf" spellings, no thousands separators — a caller number is either a
# plain decimal or it is rejected.
_NUMERIC_TEXT_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")


class State(AgentState):
    """INS-C2-006 agent state.

    Agent-specific fields only.  All shared fields (user_input, status,
    session_id, node_history, error_log, validated_input, formatted_output,
    hitl_*, etc.) are inherited from AgentState.

    Privacy: no individual PII field values stored here — only field counts
    and derived validation results.

    Domain fields are NotRequired so the TypedDict is valid at graph
    initialisation, before any node has written its values.
    """

    # Raw application JSON/form-encoded string.
    # Required by ParseNode + UnderwritingRuleValidateNode for rule evaluation.
    # Note: this is the ONLY state field that carries raw input data;
    # all downstream fields carry derived flags/counts only.
    input_record: NotRequired[str]

    # Validated caller metadata (from the input_context channel).  Each of these
    # is an inert identifier — see is_inert_identifier().
    application_ref: NotRequired[str]
    rule_profile: NotRequired[str]
    submission_channel: NotRequired[str]
    submitter_ref: NotRequired[str]

    # Count of recognized application fields extracted by ParseNode.
    # Integer count only — never the field values themselves.
    parsed_field_count: NotRequired[int]

    # Per-field rule evaluation results as a JSON string.
    # Schema: {"field_name": "pass"|"warn"|"fail", ...}
    # Written by UnderwritingRuleValidateNode; read by RiskFlagNode + GenerateReportNode.
    validation_results: NotRequired[str]

    # Per-field reason codes as a JSON string (internal — never rendered).
    # Schema: {"field_name": "missing"|"not_finite"|"below_minimum"|...}
    # Written by UnderwritingRuleValidateNode; read by RiskFlagNode.
    validation_reasons: NotRequired[str]

    # List of risk flag labels as a JSON string.
    # Schema: ["INCOMPLETE_APPLICATION", "ELIGIBILITY_FAIL", ...]
    # Written by RiskFlagNode; read by GenerateReportNode.
    risk_flags: NotRequired[str]

    # High-level underwriting decision: "PASS" | "NIGO" | "REJECT".
    # Written by GenerateReportNode.
    overall_status: NotRequired[str]

    # Remediation steps for NIGO fields as a JSON string.
    # Schema: ["Provide policy_type ...", ...]
    # Written by GenerateReportNode.
    remediation_suggestions: NotRequired[str]

    # Formatted validation report text (field-level, no raw PII values).
    # Written by GenerateReportNode; read by PostProcessNode for the output scan.
    report_output: NotRequired[str]

    # Human-readable error detail (set on ERROR path).
    error_message: NotRequired[str]
    error_code: Optional[str]


def to_json(obj: Any) -> str:
    """Serialize obj to a compact JSON string (State fields are strings)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def from_json(text: str) -> Any:
    """Deserialize a JSON string from State (returns {} on empty/invalid)."""
    if not text:
        return {}
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}


def finite_in_range(value: Any, lo: float, hi: float) -> float | None:
    """Parse a caller-controlled number defensively; fail CLOSED.

    Accepts int, float, and a plain decimal string (form-encoded submissions
    arrive as text).  Rejects, in order:

    * ``bool`` — it is an ``int`` subclass, so ``True`` would otherwise parse
      as 1 and quietly satisfy a numeric field;
    * anything that is not a number or a plain-decimal string — exponent
      notation and the literal spellings ``nan`` / ``inf`` never parse here;
    * NaN and +/-Infinity — they survive ``float()`` but every ordered
      comparison against them evaluates False, so a bound built on such a
      comparison would silently pass everything through;
    * values outside ``[lo, hi]``.

    Returns the parsed number, or None when the value must be refused.  Callers
    treat None as a validation failure — never as "no opinion".
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not _NUMERIC_TEXT_RE.match(text):
            return None
        try:
            num = float(text)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(num):
        return None
    if num < lo or num > hi:
        return None
    return num


def is_inert_identifier(value: Any) -> bool:
    """True when value is a string safe to render into an external document.

    Inert means: lowercase letters, digits and underscores, 1-32 characters.
    Anything else — punctuation, markup, whitespace, prose — is refused, so a
    caller-supplied string can never carry renderable content into the report.
    """
    return isinstance(value, str) and bool(_INERT_IDENTIFIER_RE.match(value))
