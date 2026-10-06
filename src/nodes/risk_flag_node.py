"""AgentCore Platform v1.0 — INS-C2-006 RiskFlagNode."""

# Turns the rule-evaluation outcome into risk flags — high-level signals derived
# purely from why each field passed or failed, never from raw customer values.
#
# Flag taxonomy (src/schemas/report_contract.py):
#   INCOMPLETE_APPLICATION  — a required field is missing
#   INVALID_POLICY_TYPE     — policy_type is not a supported product
#   ELIGIBILITY_FAIL        — age outside the profile's eligibility window
#   COVERAGE_BELOW_MINIMUM  — coverage below the profile's insurable floor
#   COVERAGE_ABOVE_CEILING  — coverage above what this profile may quote
#   HIGH_RISK_INDICATOR     — coverage at or above the specialist-review mark
#   INVALID_NUMERIC         — a numeric field would not parse finitely
#   FORMAT_ERROR            — an optional field failed its format/limit check
#
# Privacy contract:
#   Flags are derived from field-level reason CODES — no raw customer data
#   enters this node.
#
# Audit: emits a trace event listing the triggered flags (no field values).
#
# Node contract:
#   - Extend FunctionNode; implement execute(state) -> dict
#   - Return ONLY changed state keys
#   - required_trust_level = ANONYMOUS

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.report_contract import (
    FLAG_COVERAGE_ABOVE_CEILING,
    FLAG_COVERAGE_BELOW_MINIMUM,
    FLAG_ELIGIBILITY_FAIL,
    FLAG_FORMAT_ERROR,
    FLAG_HIGH_RISK_INDICATOR,
    FLAG_INCOMPLETE_APPLICATION,
    FLAG_INVALID_NUMERIC,
    FLAG_INVALID_POLICY_TYPE,
    REASON_ABOVE_CEILING,
    REASON_ABOVE_LIMIT,
    REASON_ABOVE_THRESHOLD,
    REASON_BELOW_MINIMUM,
    REASON_FORMAT_MISMATCH,
    REASON_MISSING,
    REASON_NOT_FINITE,
    REASON_OUT_OF_WINDOW,
    REASON_UNSUPPORTED_VALUE,
)
from src.schemas.state import to_json

# (field, reason) -> flag. A field-specific entry wins; the wildcard entries
# below cover reasons that mean the same thing on any field.
_SPECIFIC_FLAGS: dict[tuple[str, str], str] = {
    ("policy_type", REASON_UNSUPPORTED_VALUE): FLAG_INVALID_POLICY_TYPE,
    ("age_at_application", REASON_OUT_OF_WINDOW): FLAG_ELIGIBILITY_FAIL,
    ("coverage_amount", REASON_BELOW_MINIMUM): FLAG_COVERAGE_BELOW_MINIMUM,
    ("coverage_amount", REASON_ABOVE_CEILING): FLAG_COVERAGE_ABOVE_CEILING,
    ("coverage_amount", REASON_ABOVE_THRESHOLD): FLAG_HIGH_RISK_INDICATOR,
}

_REASON_FLAGS: dict[str, str] = {
    REASON_MISSING: FLAG_INCOMPLETE_APPLICATION,
    REASON_NOT_FINITE: FLAG_INVALID_NUMERIC,
    REASON_UNSUPPORTED_VALUE: FLAG_FORMAT_ERROR,
    REASON_FORMAT_MISMATCH: FLAG_FORMAT_ERROR,
    REASON_ABOVE_LIMIT: FLAG_FORMAT_ERROR,
}


def _evaluate_risk_flags(validation_results: dict[str, str], validation_reasons: dict[str, str]) -> list[str]:
    """Derive risk flags from the per-field results and their reason codes.

    Returns a list of flag labels in first-triggered order, without duplicates.
    """
    flags: list[str] = []
    seen: set[str] = set()

    def add(flag: str) -> None:
        if flag not in seen:
            flags.append(flag)
            seen.add(flag)

    for field, result in validation_results.items():
        if result not in ("fail", "warn"):
            continue
        reason = validation_reasons.get(field, "")
        specific = _SPECIFIC_FLAGS.get((field, reason))
        if specific:
            add(specific)
            continue
        generic = _REASON_FLAGS.get(reason)
        if generic:
            add(generic)
        else:
            # A non-pass result with no mapped reason still has to surface.
            add(FLAG_FORMAT_ERROR)

    return flags


class RiskFlagNode(FunctionNode):
    """Evaluates the rule outcome and produces risk flags.

    Reads validation_results and validation_reasons (JSON strings) from State
    and writes risk_flags (JSON list string).

    Trust level ANONYMOUS: the caller boundary lives on PreProcessNode.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        validation_results_raw: str = state.get("validation_results", "")

        if not validation_results_raw:
            emit_trace_event(
                "ins_c2_006.risk_flag.error",
                {"reason": "no_validation_results"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    "RiskFlagNode: validation_results missing — " "UnderwritingRuleValidateNode may have failed"
                ],
                "risk_flags": to_json([]),
            }

        try:
            validation_results: dict[str, str] = json.loads(validation_results_raw)
            validation_reasons: dict[str, str] = json.loads(state.get("validation_reasons", "") or "{}")
        except (json.JSONDecodeError, TypeError) as exc:
            emit_trace_event(
                "ins_c2_006.risk_flag.error",
                {"reason": "json_parse_error", "detail": str(exc)},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"RiskFlagNode: could not parse the rule outcome — {exc}"],
                "risk_flags": to_json([]),
            }

        flags = _evaluate_risk_flags(validation_results, validation_reasons)

        emit_trace_event(
            "ins_c2_006.risk_flag.complete",
            {
                "flags_triggered": flags,
                "flag_count": len(flags),
            },
            state,
        )

        return {
            "risk_flags": to_json(flags),
            "status": AgentStatus.SUCCESS.value,
        }
