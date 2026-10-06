"""AgentCore Platform v1.0 — INS-C2-006 PreProcessNode (pre_process backbone)."""

# The external input boundary. Two responsibilities:
#
#   1. Application screening — trust gate (VERIFIED_EXTERNAL; inner domain
#      nodes are ANONYMOUS because the boundary is enforced here), empty check,
#      size ceiling, injection scan, and the [MASKED] sentinel the framework
#      input gate substitutes when it detects personal data.
#   2. Caller-metadata validation — every field read from the caller-supplied
#      input_context channel is checked against explicit bounds before any
#      later node may consume it. Invalid values fail CLOSED: the request is
#      refused naming the field, never echoing the value. Absent fields fall
#      back to documented defaults.
#
# This node owns the guarantees rather than relying on the framework gate in
# front of it: the injection scan below runs inside execute(), so calling
# execute() directly still refuses the payload.
#
# Every code path emits an audit event.
#
# Node contract:
#   - Extend FunctionNode; implement execute(state) -> dict (partial update)
#   - Return ONLY the fields this node changes (never the full state)
#   - Return AgentStatus enum constants — never plain strings
#   - Read input_context via state.get("input_context", {}) — read-only

import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, TOO_LONG

from src.schemas.state import is_inert_identifier
from src.services.service import RuleSetError, get_rule_set

# Injection marker scan — prompt-injection and SQL-injection forms seen in
# adversarial application submissions.
_INJECTION_MARKERS: tuple[str, ...] = (
    "ignore previous instructions",
    "ignore all instructions",
    "you are now",
    "act as",
    "system prompt",
    "jailbreak",
    "drop table",
    "select * from",
    "union select",
    "' or '1'='1",
    "'; --",
)

# Size ceiling: application payloads larger than 64 KB are suspicious.
_MAX_INPUT_CHARS: int = 65_536

# The framework input gate replaces detected personal data with this sentinel
# before execute() runs. A payload reduced to nothing but the sentinel has lost
# all of its application content.
_MASKED_PATTERN = re.compile(r"^\s*\[MASKED\]\s*$")

# ── Caller-metadata contract (input_context) ─────────────────────────────────
#
# The caller may pass structured submission metadata alongside the application.
# Every consumed field is validated here; nothing outside this contract can
# influence the pipeline, and an unrecognised caller key is refused rather than
# ignored (see the runtime-supplied set below for the one exception, which the
# caller does not author). Free text is not accepted on any field — identifiers only — so
# caller metadata can never carry renderable prose into the report.
#
#   channel          identifier [a-z0-9_]{1,32}   default "unknown"  (audit only)
#   submitter_ref    identifier [a-z0-9_]{1,32}   default ""         (audit only)
#   application_ref  identifier [a-z0-9_]{1,32}   default ""         (rendered)
#   rule_profile     one of the profiles declared in config/rules.yaml
#
_METADATA_IDENTIFIER_FIELDS: tuple[str, ...] = ("channel", "submitter_ref", "application_ref")
_ALLOWED_METADATA_KEYS: frozenset[str] = frozenset({*_METADATA_IDENTIFIER_FIELDS, "rule_profile"})

# Fields the hosting runtime places on the context channel itself. They are not
# part of the caller-metadata contract above and this node consumes none of
# them, but refusing them would refuse every invocation served that way — the
# caller cannot remove a field it never added. They are accepted and ignored,
# which is sound precisely because no constraint is attached to them: nothing
# is promised about them, so there is nothing a caller could be misled about.
_RUNTIME_METADATA_KEYS: frozenset[str] = frozenset({"conversation_history"})

_ACCEPTED_METADATA_KEYS: frozenset[str] = _ALLOWED_METADATA_KEYS | _RUNTIME_METADATA_KEYS

_DEFAULT_CHANNEL = "unknown"


def _contains_injection(text: str) -> bool:
    """Return True when the payload carries an injection marker."""
    lower = text.lower()
    return any(marker in lower for marker in _INJECTION_MARKERS)


def _reject_metadata(field: str, state: dict[str, Any]) -> dict[str, Any]:
    """Fail closed on an invalid caller-metadata field.

    Names the field only — the rejected value never reaches the error log or
    the response.
    """
    emit_trace_event(
        "ins_c2_006.pre_process.rejected",
        {"reason": "invalid_caller_metadata", "field": field},
        state,
    )
    emit_progress(INPUT_REJECTED)
    return {
        "status": AgentStatus.SUCCESS.value,
        "error_code": "INVALID_REQUEST",
        "error_log": [f"PreProcessNode: input_context field '{field}' failed validation"],
        "error_message": f"Invalid value for caller metadata field '{field}'",
    }


class PreProcessNode(FunctionNode):
    """Input boundary for INS-C2-006.

    Validates the incoming insurance application payload and the caller
    metadata before either enters the domain pipeline (ParseNode →
    UnderwritingRuleValidateNode → RiskFlagNode → GenerateReportNode →
    PostProcessNode).

    Assigned to the backbone `pre_process` slot.
    Trust level VERIFIED_EXTERNAL: only authenticated callers reach the
    pipeline. Inner domain nodes are ANONYMOUS — no second gate needed.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        user_input: Any = state.get("user_input", "")
        input_context: Any = state.get("input_context", {})

        if not isinstance(input_context, dict):
            return _reject_metadata("input_context", state)

        if not isinstance(user_input, str):
            emit_trace_event(
                "ins_c2_006.pre_process.rejected",
                {"reason": "invalid_input_type", "type": type(user_input).__name__},
                state,
            )
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": ["PreProcessNode: user_input must be a string"],
                "error_message": "Invalid input type: submit the application as text",
            }

        # ── Non-empty application ────────────────────────────────────────────
        if not user_input.strip():
            emit_trace_event(
                "ins_c2_006.pre_process.rejected",
                {"reason": "empty_input"},
                state,
            )
            emit_progress(EMPTY_INPUT)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "EMPTY_INPUT",
                "error_log": [
                    "PreProcessNode: user_input is empty or missing — "
                    "provide a JSON or form-encoded insurance application"
                ],
                "error_message": "Empty input: insurance application data is required",
            }

        stripped = user_input.strip()

        # ── Fully-masked payload ─────────────────────────────────────────────
        # The framework input gate replaced every detected personal-data span,
        # leaving only the sentinel: there is no application left to evaluate.
        if _MASKED_PATTERN.match(stripped):
            emit_trace_event(
                "ins_c2_006.pre_process.rejected",
                {"reason": "fully_masked_input"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    "PreProcessNode: the input gate masked the whole payload; "
                    "a fully masked application cannot be evaluated"
                ],
                "error_message": "Input fully masked: resubmit via a trusted channel",
            }

        # ── Size ceiling ─────────────────────────────────────────────────────
        if len(stripped) > _MAX_INPUT_CHARS:
            emit_trace_event(
                "ins_c2_006.pre_process.rejected",
                {"reason": "input_too_large", "size": len(stripped)},
                state,
            )
            emit_progress(TOO_LONG)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "QUESTION_TOO_LONG",
                "error_log": [
                    f"PreProcessNode: input size {len(stripped)} chars exceeds "
                    f"ceiling {_MAX_INPUT_CHARS} — possible data-dump attack"
                ],
                "error_message": "Input too large: reduce application payload size",
            }

        # ── Injection scan ───────────────────────────────────────────────────
        # Enforced here, in the node that owns the caller contract, so the
        # refusal holds whether or not a framework gate ran first.
        if _contains_injection(stripped):
            emit_trace_event(
                "ins_c2_006.pre_process.rejected",
                {"reason": "injection_detected"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    "PreProcessNode: injection pattern detected in application payload — "
                    "resubmit a clean application"
                ],
                "error_message": "Injection pattern detected in input",
            }

        # ── Caller metadata (fail CLOSED per field) ──────────────────────────
        unknown_keys = sorted(set(input_context) - _ACCEPTED_METADATA_KEYS)
        if unknown_keys:
            return _reject_metadata(unknown_keys[0], state)

        metadata: dict[str, str] = {"channel": _DEFAULT_CHANNEL, "submitter_ref": "", "application_ref": ""}
        for field in _METADATA_IDENTIFIER_FIELDS:
            if field in input_context:
                if not is_inert_identifier(input_context[field]):
                    return _reject_metadata(field, state)
                metadata[field] = input_context[field]

        try:
            rule_set = get_rule_set()
        except RuleSetError as exc:
            emit_trace_event(
                "ins_c2_006.pre_process.rejected",
                {"reason": "rule_set_invalid"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: underwriting rule set unusable — {exc}"],
                "error_message": "Underwriting rules are not currently available",
            }

        rule_profile = rule_set.default_profile
        if "rule_profile" in input_context:
            candidate = input_context["rule_profile"]
            if not is_inert_identifier(candidate) or candidate not in rule_set.profiles:
                return _reject_metadata("rule_profile", state)
            rule_profile = candidate

        emit_trace_event(
            "ins_c2_006.pre_process.accepted",
            {
                "input_size": len(stripped),
                "channel": metadata["channel"],
                "submitter_ref": metadata["submitter_ref"],
                "rule_profile": rule_profile,
            },
            state,
        )

        return {
            # validated_input: canonical reference for audit; raw application blob.
            # input_record: same content — dedicated state field for domain nodes.
            "validated_input": stripped,
            "input_record": stripped,
            "submission_channel": metadata["channel"],
            "submitter_ref": metadata["submitter_ref"],
            "application_ref": metadata["application_ref"],
            "rule_profile": rule_profile,
            "status": AgentStatus.SUCCESS.value,
        }
