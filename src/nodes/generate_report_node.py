"""AgentCore Platform v1.0 — INS-C2-006 GenerateReportNode."""

# Compiles the rule outcome and risk flags into the underwriting validation
# report.
#
# Output:
#   overall_status          — "PASS" | "NIGO" | "REJECT"
#   remediation_suggestions — JSON list of correction instructions
#   report_output           — the formatted report text
#
# Decision logic:
#   REJECT  — a flag the applicant cannot correct within this profile:
#             age outside the eligibility window, or coverage outside the band
#             the profile is authorised to quote.
#   NIGO    — any other fail/warn result: correctable, resubmit after fixing.
#   PASS    — every evaluated field passed.
#
# Output invariant:
#   The report is assembled ONLY from the vocabulary in
#   src/schemas/report_contract.py — fixed headings, evaluated field names,
#   three result labels, the flag taxonomy, the remediation sentences for the
#   active threshold profile, and the caller's application reference, which was
#   validated as an inert identifier before it reached this node. No applicant
#   value is ever rendered. PostProcessNode re-derives that permitted vocabulary
#   from the same rule set and refuses any report that steps outside it.
#
# Audit: emits a trace event with the decision and counts.
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
    FIELD_SECTION,
    FLAG_COVERAGE_ABOVE_CEILING,
    FLAG_COVERAGE_BELOW_MINIMUM,
    FLAG_ELIGIBILITY_FAIL,
    FLAG_SECTION,
    NO_FLAGS_LINE,
    REFERENCE_ABSENT,
    REMEDIATION_SECTION,
    REPORT_FOOTER,
    REPORT_TITLE,
    RESULT_FAIL,
    RESULT_WARN,
    STATUS_NIGO,
    STATUS_PASS,
    STATUS_REJECT,
    generic_remediation,
    remediation_catalogue,
)
from src.schemas.state import to_json
from src.services.service import Profile, RuleSet, RuleSetError, get_rule_set

# Flags the applicant cannot correct by resubmitting under the same profile.
_REJECT_FLAGS: frozenset[str] = frozenset(
    {FLAG_ELIGIBILITY_FAIL, FLAG_COVERAGE_BELOW_MINIMUM, FLAG_COVERAGE_ABOVE_CEILING}
)


def _build_report(
    application_ref: str,
    profile_name: str,
    validation_results: dict[str, str],
    risk_flags: list[str],
    overall_status: str,
    suggestions: list[str],
) -> str:
    """Build the formatted report from the closed report vocabulary."""
    lines: list[str] = [
        REPORT_TITLE,
        f"Application Reference: {application_ref or REFERENCE_ABSENT}",
        f"Rule Profile: {profile_name}",
        f"Overall Status: {overall_status}",
        "",
        FIELD_SECTION,
    ]

    for field, result in validation_results.items():
        lines.append(f"  {field}: {result.upper()}")

    lines.append("")
    lines.append(FLAG_SECTION)
    if risk_flags:
        lines.extend(f"  [{flag}]" for flag in risk_flags)
    else:
        lines.append(NO_FLAGS_LINE)

    if suggestions:
        lines.append("")
        lines.append(REMEDIATION_SECTION)
        for index, suggestion in enumerate(suggestions, start=1):
            lines.append(f"  {index}. {suggestion}")

    lines.append("")
    lines.append(REPORT_FOOTER)

    return "\n".join(lines)


def _build_suggestions(validation_results: dict[str, str], rule_set: RuleSet, profile: Profile) -> list[str]:
    """Remediation sentences for every field that did not pass."""
    catalogue = remediation_catalogue(rule_set, profile)
    suggestions: list[str] = []
    for field, result in validation_results.items():
        if result not in (RESULT_FAIL, RESULT_WARN):
            continue
        suggestions.append(catalogue.get(field) or generic_remediation(field, result))
    return suggestions


class GenerateReportNode(FunctionNode):
    """Compiles the rule outcome and risk flags into the validation report.

    Writes overall_status, remediation_suggestions and report_output to State.
    The report text carries field NAMES, result LABELS, flag labels and the
    caller's inert application reference only.

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
        risk_flags_raw: str = state.get("risk_flags", "")

        if not validation_results_raw:
            emit_trace_event(
                "ins_c2_006.generate_report.error",
                {"reason": "no_validation_results"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["GenerateReportNode: validation_results missing"],
            }

        try:
            validation_results: dict[str, str] = json.loads(validation_results_raw)
        except (json.JSONDecodeError, TypeError) as exc:
            emit_trace_event(
                "ins_c2_006.generate_report.error",
                {"reason": "json_parse_error", "detail": str(exc)},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"GenerateReportNode: could not parse the rule outcome — {exc}"],
            }

        try:
            risk_flags: list[str] = json.loads(risk_flags_raw) if risk_flags_raw else []
        except (json.JSONDecodeError, TypeError):
            risk_flags = []

        try:
            rule_set = get_rule_set()
        except RuleSetError as exc:
            emit_trace_event(
                "ins_c2_006.generate_report.error",
                {"reason": "rule_set_invalid"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"GenerateReportNode: underwriting rule set unusable — {exc}"],
            }
        profile = rule_set.profile(state.get("rule_profile"))

        if any(flag in risk_flags for flag in _REJECT_FLAGS):
            overall_status = STATUS_REJECT
        elif any(result in (RESULT_FAIL, RESULT_WARN) for result in validation_results.values()):
            overall_status = STATUS_NIGO
        else:
            overall_status = STATUS_PASS

        suggestions = (
            _build_suggestions(validation_results, rule_set, profile)
            if overall_status in (STATUS_NIGO, STATUS_REJECT)
            else []
        )

        report_text = _build_report(
            application_ref=str(state.get("application_ref", "")),
            profile_name=profile.name,
            validation_results=validation_results,
            risk_flags=risk_flags,
            overall_status=overall_status,
            suggestions=suggestions,
        )

        emit_trace_event(
            "ins_c2_006.generate_report.complete",
            {
                "overall_status": overall_status,
                "profile": profile.name,
                "risk_flag_count": len(risk_flags),
                "suggestion_count": len(suggestions),
            },
            state,
        )

        return {
            "overall_status": overall_status,
            "remediation_suggestions": to_json(suggestions),
            "report_output": report_text,
            "status": AgentStatus.SUCCESS.value,
        }
