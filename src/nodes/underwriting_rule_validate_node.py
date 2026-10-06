"""AgentCore Platform v1.0 — INS-C2-006 UnderwritingRuleValidateNode."""

# Applies the underwriting rules to the raw application record and produces
# per-field pass/warn/fail results.
#
# Rule categories (all sourced from config/rules.yaml at runtime):
#   1. Completeness checks — required fields must be present and non-empty.
#   2. Format checks — numeric types, code formats, enumerated values.
#   3. Business rule checks — age eligibility, coverage band, structural limits.
#
# Numeric handling is the security-relevant part. Every number in the
# application is caller-controlled, so each one goes through finite_in_range()
# instead of a bare float()/int(): NaN and Infinity survive float() but compare
# False against every bound, which would mark an unbounded coverage amount
# "pass" — a silent fail-open on the exact decision this agent exists to make.
# A value that will not parse finitely and in range fails CLOSED.
#
# Privacy contract:
#   Raw application field VALUES are parsed locally in execute() and NEVER
#   written to State. Only validation_results ({"field_name": result}) is
#   persisted — field-level result flags, not values.
#
# Audit: emits a per-rule aggregate event (counts only).
#
# Node contract:
#   - Extend FunctionNode; implement execute(state) -> dict
#   - Return ONLY changed state keys
#   - required_trust_level = ANONYMOUS

import json
from typing import Any, ClassVar
from urllib.parse import parse_qs

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.report_contract import (
    REASON_ABOVE_CEILING,
    REASON_ABOVE_LIMIT,
    REASON_ABOVE_THRESHOLD,
    REASON_BELOW_MINIMUM,
    REASON_FORMAT_MISMATCH,
    REASON_MISSING,
    REASON_NOT_FINITE,
    REASON_OK,
    REASON_OUT_OF_WINDOW,
    REASON_UNSUPPORTED_VALUE,
    RESULT_FAIL,
    RESULT_PASS,
    RESULT_WARN,
)
from src.schemas.state import finite_in_range, to_json
from src.services.service import Profile, RuleSet, RuleSetError, get_rule_set

# Outer parse bound for a monetary amount, independent of the profile band: a
# value beyond this is not a plausible coverage figure at all.
_COVERAGE_PARSE_CEILING = 1e12
# Outer parse bounds for the structural fields.
_AGE_PARSE_CEILING = 130.0
_TERM_PARSE_CEILING = 120.0
_BENEFICIARY_PARSE_CEILING = 100.0


def _is_present(value: Any) -> bool:
    """A required field counts as supplied when it is not empty/whitespace."""
    return value is not None and str(value).strip() != ""


def _validate_application(
    parsed: dict[str, Any], rule_set: RuleSet, profile: Profile
) -> tuple[dict[str, str], dict[str, str]]:
    """Apply the underwriting rules to a parsed application.

    Returns (results, reasons): results maps field_name -> "pass" | "warn" |
    "fail" and is what the report renders; reasons maps field_name -> a reason
    code that RiskFlagNode turns into risk flags. Raw field values are consumed
    locally; neither returned mapping contains them.
    """
    results: dict[str, str] = {}
    reasons: dict[str, str] = {}

    def record(field: str, result: str, reason: str) -> None:
        results[field] = result
        reasons[field] = reason

    # ── 1. Completeness ───────────────────────────────────────────────────
    for field in sorted(rule_set.required_fields):
        if _is_present(parsed.get(field)):
            record(field, RESULT_PASS, REASON_OK)
        else:
            record(field, RESULT_FAIL, REASON_MISSING)

    # ── 2. policy_type — enumerated product list ──────────────────────────
    policy_type = parsed.get("policy_type")
    if _is_present(policy_type):
        if str(policy_type) in rule_set.policy_types:
            record("policy_type", RESULT_PASS, REASON_OK)
        else:
            record("policy_type", RESULT_FAIL, REASON_UNSUPPORTED_VALUE)

    # ── 3. coverage_amount — finite, then inside the profile band ─────────
    coverage_raw = parsed.get("coverage_amount")
    if _is_present(coverage_raw):
        coverage = finite_in_range(coverage_raw, 0.0, _COVERAGE_PARSE_CEILING)
        if coverage is None:
            # Non-numeric, non-finite, or beyond any plausible amount. Refusing
            # here is what stops a NaN amount from comparing False against every
            # band bound and being reported as acceptable.
            record("coverage_amount", RESULT_FAIL, REASON_NOT_FINITE)
        elif coverage < profile.coverage_min:
            record("coverage_amount", RESULT_FAIL, REASON_BELOW_MINIMUM)
        elif coverage > profile.coverage_max:
            record("coverage_amount", RESULT_FAIL, REASON_ABOVE_CEILING)
        elif coverage >= profile.coverage_warn:
            # Inside the band but at or above the specialist-review threshold.
            record("coverage_amount", RESULT_WARN, REASON_ABOVE_THRESHOLD)
        else:
            record("coverage_amount", RESULT_PASS, REASON_OK)

    # ── 4. age_at_application — finite whole number inside the window ──────
    age_raw = parsed.get("age_at_application")
    if _is_present(age_raw):
        age = finite_in_range(age_raw, 0.0, _AGE_PARSE_CEILING)
        if age is None or age != int(age):
            record("age_at_application", RESULT_FAIL, REASON_NOT_FINITE)
        elif age < profile.age_min or age > profile.age_max:
            record("age_at_application", RESULT_FAIL, REASON_OUT_OF_WINDOW)
        else:
            record("age_at_application", RESULT_PASS, REASON_OK)

    # ── 5. Optional fields ────────────────────────────────────────────────
    occupation_code = parsed.get("occupation_code")
    if _is_present(occupation_code):
        if rule_set.occupation_code_pattern.match(str(occupation_code)):
            record("occupation_code", RESULT_PASS, REASON_OK)
        else:
            record("occupation_code", RESULT_WARN, REASON_FORMAT_MISMATCH)

    premium_class = parsed.get("premium_class")
    if _is_present(premium_class):
        if str(premium_class).lower() in rule_set.premium_classes:
            record("premium_class", RESULT_PASS, REASON_OK)
        else:
            record("premium_class", RESULT_WARN, REASON_UNSUPPORTED_VALUE)

    term_raw = parsed.get("policy_term_years")
    if _is_present(term_raw):
        term = finite_in_range(term_raw, 0.0, _TERM_PARSE_CEILING)
        if term is None or term != int(term):
            record("policy_term_years", RESULT_FAIL, REASON_NOT_FINITE)
        elif term < profile.policy_term_years_min or term > profile.policy_term_years_max:
            record("policy_term_years", RESULT_WARN, REASON_ABOVE_LIMIT)
        else:
            record("policy_term_years", RESULT_PASS, REASON_OK)

    beneficiaries_raw = parsed.get("beneficiary_count")
    if _is_present(beneficiaries_raw):
        beneficiaries = finite_in_range(beneficiaries_raw, 0.0, _BENEFICIARY_PARSE_CEILING)
        if beneficiaries is None or beneficiaries != int(beneficiaries):
            record("beneficiary_count", RESULT_FAIL, REASON_NOT_FINITE)
        elif beneficiaries > profile.beneficiary_count_max:
            record("beneficiary_count", RESULT_WARN, REASON_ABOVE_LIMIT)
        else:
            record("beneficiary_count", RESULT_PASS, REASON_OK)

    return results, reasons


def _parse_record(input_record: str) -> dict[str, Any]:
    """Parse input_record to a dict; JSON first, then form-encoded.

    Raises ValueError on failure.
    """
    stripped = input_record.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
        raise ValueError("JSON root must be a dict")
    qs = parse_qs(stripped, keep_blank_values=True)
    if qs:
        return {k: v[0] for k, v in qs.items()}
    raise ValueError("Could not parse as JSON or form-encoded")


class UnderwritingRuleValidateNode(FunctionNode):
    """Applies the config/rules.yaml rule set to the application record.

    Reads input_record from State, parses locally, applies the rules for the
    profile the caller selected, and writes validation_results (per-field
    pass/warn/fail JSON string). Raw field values remain local to execute() —
    never persisted.

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
                "ins_c2_006.uw_validate.error",
                {"reason": "no_input_record"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["UnderwritingRuleValidateNode: input_record missing"],
                "validation_results": to_json({}),
                "validation_reasons": to_json({}),
            }

        try:
            rule_set = get_rule_set()
        except RuleSetError as exc:
            emit_trace_event(
                "ins_c2_006.uw_validate.error",
                {"reason": "rule_set_invalid"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"UnderwritingRuleValidateNode: underwriting rule set unusable — {exc}"],
                "validation_results": to_json({}),
                "validation_reasons": to_json({}),
            }
        profile = rule_set.profile(state.get("rule_profile"))

        try:
            parsed_dict = _parse_record(input_record)
        except (ValueError, json.JSONDecodeError) as exc:
            emit_trace_event(
                "ins_c2_006.uw_validate.error",
                {"reason": "parse_failure", "detail": str(exc)},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"UnderwritingRuleValidateNode: parse error — {exc}"],
                "validation_results": to_json({}),
                "validation_reasons": to_json({}),
            }

        # Apply the rules locally — raw values are not persisted.
        results, reasons = _validate_application(parsed_dict, rule_set, profile)

        emit_trace_event(
            "ins_c2_006.uw_validate.complete",
            {
                "profile": profile.name,
                "total_fields_evaluated": len(results),
                "pass": sum(1 for v in results.values() if v == RESULT_PASS),
                "warn": sum(1 for v in results.values() if v == RESULT_WARN),
                "fail": sum(1 for v in results.values() if v == RESULT_FAIL),
            },
            state,
        )

        return {
            "validation_results": to_json(results),
            "validation_reasons": to_json(reasons),
            "status": AgentStatus.SUCCESS.value,
        }
