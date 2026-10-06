"""AgentCore Platform v1.0 — INS-C2-006 ParseNode (main backbone slot)."""

# Parses the raw insurance application from `input_record` (JSON or
# form-encoded) into a structured field dict — LOCAL to execute() only.
#
# Privacy contract:
#   Raw field VALUES are used within this node only (local parse result).
#   They are NEVER written to State. Only the field COUNT
#   (parsed_field_count) is persisted, keeping applicant data out of the
#   checkpointed state.
#
# Audit: emits a trace event with count + format metadata (no field values).
#
# Node contract:
#   - Extend FunctionNode; implement execute(state) -> dict
#   - Return ONLY changed state keys (never the full state)
#   - required_trust_level = ANONYMOUS (inner domain node; the caller boundary
#     is enforced by PreProcessNode)

import json
from typing import Any, ClassVar
from urllib.parse import parse_qs

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import INPUT_REJECTED

from src.schemas.report_contract import EVALUATED_FIELDS

# Application field names recognised by this agent: the fields the underwriting
# rules evaluate, plus the informational fields an application may carry.
_KNOWN_FIELDS: frozenset[str] = frozenset(
    {
        *EVALUATED_FIELDS,
        "payment_frequency",
        "health_declaration",
        "smoker_status",
    }
)


def _parse_application(raw: str) -> tuple[dict[str, Any], str]:
    """Attempt JSON parse, then form-encoded parse.

    Returns (parsed_dict, format_detected) where format_detected is
    "json" or "form_encoded".  Raises ValueError on parse failure.
    """
    stripped = raw.strip()

    # Try JSON first (most common format from API callers)
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed, "json"
            raise ValueError("JSON root must be an object (dict)")
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON parse error: {exc}") from exc

    # Fall back to application/x-www-form-urlencoded
    try:
        qs = parse_qs(stripped, keep_blank_values=True)
        if qs:
            # parse_qs returns lists; take first value for each key
            return {k: v[0] for k, v in qs.items()}, "form_encoded"
        raise ValueError("Form-encoded parse produced empty result")
    except Exception as exc:
        raise ValueError(f"Form-encoded parse error: {exc}") from exc


class ParseNode(FunctionNode):
    """Parses the raw insurance application into a structured field dict.

    Assigned to the backbone `main` slot.
    Handles JSON and application/x-www-form-urlencoded formats.
    Writes only parsed_field_count (int) to State — no raw field values.

    Trust level ANONYMOUS: the caller boundary lives on PreProcessNode.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        input_record: str = state.get("input_record", "") or state.get("validated_input", "")

        if not input_record:
            emit_trace_event(
                "ins_c2_006.parse.error",
                {"reason": "no_input_record"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ParseNode: input_record is empty — PreProcessNode may have failed"],
                "parsed_field_count": 0,
            }

        try:
            # Parse application locally — raw field VALUES stay in local scope only.
            parsed_dict, fmt = _parse_application(input_record)
        except ValueError as exc:
            emit_trace_event(
                "ins_c2_006.parse.error",
                {"reason": "parse_failure", "detail": str(exc)},
                state,
            )
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [f"ParseNode: failed to parse application — {exc}"],
                "parsed_field_count": 0,
            }

        # Count only recognized field names (not raw values).
        recognized = [k for k in parsed_dict if k in _KNOWN_FIELDS]
        field_count = len(recognized)

        # Audit log — field COUNT and format only; no field values.
        emit_trace_event(
            "ins_c2_006.parse.complete",
            {
                "format": fmt,
                "total_fields": len(parsed_dict),
                "recognized_fields": field_count,
            },
            state,
        )

        return {
            "parsed_field_count": field_count,
            "status": AgentStatus.SUCCESS.value,
        }
