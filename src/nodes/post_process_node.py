"""AgentCore Platform v1.0 — INS-C2-006 PostProcessNode (post_process backbone)."""

# The external-output boundary. The agent's documented output invariant is a
# privacy one:
#
#   the validation report carries field NAMES, result LABELS, risk-flag labels,
#   remediation sentences derived from the configured thresholds, and the
#   caller's inert application reference — and nothing else. No applicant value
#   ever reaches the caller.
#
# This agent renders no caller-derived monetary aggregates: the only figures in
# the report are the configured underwriting thresholds, which the vocabulary
# check below pins to the active profile. There is therefore no rounding grid to
# enforce — the invariant to enforce is the vocabulary itself, and it is
# enforced for EVERY line, not for the convenient ones.
#
# Four independent layers, in this order:
#
#   (1) structural integrity — non-empty, mandatory footer present, size
#       ceiling. A truncated report is not a safe report;
#   (2) pattern scan — credential and personal-data forms anywhere in the text.
#       This runs FIRST among the content checks and on the untouched report:
#       a scan that runs after something has rewritten the text can miss a
#       pattern whose digits were altered;
#   (3) verbatim application text — any substantial span of the submitted
#       application appearing in the report means raw applicant data reached
#       the external surface;
#   (4) vocabulary conformance — every line must be one the report contract
#       authorises for the active rule profile. Any line outside that set is
#       content the generator was never permitted to produce.
#
# Every layer fails CLOSED: the report is withheld, the node returns ERROR, and
# the layer emits its own audit event
# (ins_c2_006.post_process.{structure|pattern_scan|verbatim_scan|vocabulary}_refusal)
# alongside the overall verdict event.
#
# The gate is a MODULE-LEVEL function (_security_gate_output) called from
# execute(), not an instance method: the framework auto-wraps node instance
# methods on the real invoke path.
#
# Node contract:
#   - Extend FunctionNode; implement execute(state) -> dict
#   - required_trust_level = ANONYMOUS (the caller boundary is PreProcessNode)
#   - Return ONLY changed state keys

import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG

from src.schemas.report_contract import (
    PROFILE_LINE_RE,
    REFERENCE_LINE_RE,
    REPORT_FOOTER,
    permitted_lines,
)
from src.services.service import RuleSetError, get_rule_set

_MAX_OUTPUT_CHARS: int = 50_000  # 50 KB ceiling; oversized output = data-leakage risk

# Shortest span of the submitted application that counts as a verbatim leak.
# Short incidental overlaps (a field name, a status word) are not evidence.
_MIN_VERBATIM_SPAN: int = 12

# Credential and personal-data forms that must never appear in the report.
# Named so the audit event can say WHICH class fired without quoting the text.
_PATTERN_CHECKS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?:sk|pk|ak)-[A-Za-z0-9]{16,}"), "api_key"),
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "json_web_token"),
    (re.compile(r"Bearer\s+[A-Za-z0-9_\-.]{8,}", re.IGNORECASE), "bearer_token"),
    (
        re.compile(
            r"(?:password|passwd|secret|api[_-]?key|token|access[_-]?key|private[_-]?key)\s*[:=]\s*\S{6,}",
            re.IGNORECASE,
        ),
        "credential_assignment",
    ),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "government_id"),
    (re.compile(r"\b\d{4}[ \-]?\d{4}[ \-]?\d{4}[ \-]?\d{4}\b"), "payment_card"),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "email_address"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "date_of_birth"),
    (
        re.compile(r"(?:dob|birth[_ ]?date|health[_ ]?condition|diagnosis)\s*[:=]", re.IGNORECASE),
        "health_or_birth_field",
    ),
]


def _scan_patterns(output: str) -> str | None:
    """Return the name of the first credential/personal-data form found."""
    for pattern, name in _PATTERN_CHECKS:
        if pattern.search(output):
            return name
    return None


def _find_verbatim_application_text(output: str, state: dict[str, Any]) -> bool:
    """True when the submitted application appears verbatim in the report.

    Compares against the state fields that carry caller text. Whole-value
    containment is the check: the report is assembled from a closed vocabulary,
    so any substantial span of the submission showing up in it is a leak.
    """
    for field in ("input_record", "validated_input", "user_input"):
        value = state.get(field)
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        if len(candidate) >= _MIN_VERBATIM_SPAN and candidate in output:
            return True
    return False


def _find_unauthorised_line(output: str, profile_name: str | None) -> tuple[int, str] | None:
    """Return (line_number, reason) for the first line outside the vocabulary.

    The permitted set is rebuilt here from the rule set — not from the
    generator's output — so a generator that started rendering applicant data
    would produce lines this check never authorised.
    """
    rule_set = get_rule_set()
    profile = rule_set.profile(profile_name)
    allowed = permitted_lines(rule_set, profile)

    for number, line in enumerate(output.split("\n"), start=1):
        if line in allowed:
            continue
        if REFERENCE_LINE_RE.match(line) or PROFILE_LINE_RE.match(line):
            continue
        return number, "line is not part of the authorised report vocabulary"
    return None


def _security_gate_output(output: str, state: dict[str, Any]) -> tuple[bool, str, str]:
    """Run the output boundary over the assembled report.

    Returns (passed, layer, reason). `layer` names which check decided, so the
    audit event records the decision without quoting the report text.
    """
    if not output:
        return False, "structure", "output is empty"

    if REPORT_FOOTER not in output:
        return (
            False,
            "structure",
            (f"mandatory report footer '{REPORT_FOOTER}' absent — " "the report was truncated or never completed"),
        )

    if len(output) > _MAX_OUTPUT_CHARS:
        return (
            False,
            "structure",
            (
                f"output size ({len(output)} chars) exceeds the {_MAX_OUTPUT_CHARS}-char "
                "ceiling — possible raw data re-emission"
            ),
        )

    pattern_name = _scan_patterns(output)
    if pattern_name is not None:
        return False, "pattern_scan", f"'{pattern_name}' pattern detected in the report"

    if _find_verbatim_application_text(output, state):
        return False, "verbatim_scan", "the submitted application appears verbatim in the report"

    try:
        unauthorised = _find_unauthorised_line(output, state.get("rule_profile"))
    except RuleSetError as exc:
        return False, "vocabulary", f"underwriting rule set unusable — {exc}"
    if unauthorised is not None:
        number, reason = unauthorised
        return False, "vocabulary", f"line {number}: {reason}"

    return True, "", "output boundary passed"


# Reason code -> the sentence the caller reads. A code with no entry falls
# back to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class PostProcessNode(FunctionNode):
    """The external-output boundary for INS-C2-006.

    Reads report_output from State (set by GenerateReportNode), runs the output
    gate, and sets formatted_output for the backbone's get_output() → caller
    result dict. A gate failure withholds the report entirely.

    Assigned to the backbone `post_process` slot.
    Trust level ANONYMOUS: the caller boundary lives on PreProcessNode.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        # A run declined upstream has nothing to format. Render the reason as
        # the caller-facing body and carry the marker onward.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("post_process_degraded", {"reason": marker}, state)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
                "result": message,
                "formatted_output": message,
            }
        report_output: str | None = state.get("report_output")

        if not report_output:
            emit_trace_event(
                "ins_c2_006.post_process.error",
                {"reason": "no_report_output"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PostProcessNode: no report_output in state — GenerateReportNode may have failed"],
            }

        gate_passed, layer, gate_reason = _security_gate_output(report_output, state)

        # Each layer raises its own audit event, so a refusal is attributable to
        # one boundary check rather than to "the gate". Never the report text.
        if not gate_passed:
            emit_trace_event(
                f"ins_c2_006.post_process.{layer}_refusal",
                {"reason": gate_reason, "output_size": len(report_output)},
                state,
            )

        # The overall verdict, emitted on every path.
        emit_trace_event(
            "ins_c2_006.post_process.output_gate_verdict",
            {
                "gate_passed": gate_passed,
                "layer": layer,
                "gate_reason": gate_reason,
                "output_size": len(report_output),
                "overall_status": state.get("overall_status", ""),
            },
            state,
        )

        if not gate_passed:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PostProcessNode: output boundary refused the report — {gate_reason}"],
            }

        return {
            # AgentBaseGraph.get_output() maps state["formatted_output"] → result["output"].
            # This is the DECLARED state field; DO NOT use an undeclared "output" key.
            "formatted_output": report_output,
            "status": AgentStatus.SUCCESS.value,
        }
