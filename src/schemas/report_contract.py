"""AgentCore Platform v1.0 — INS-C2-006 validation-report contract."""

# The report this agent emits is assembled from a CLOSED vocabulary: fixed
# headings, the field names declared in the rule set, three result labels, a
# fixed risk-flag taxonomy, and remediation sentences derived from the active
# threshold profile. No applicant value ever appears in it.
#
# That is the agent's external-output invariant, and this module is where the
# vocabulary is defined so that BOTH sides can use it:
#
#   * GenerateReportNode builds the report from it;
#   * PostProcessNode independently rebuilds the permitted line set from the
#     same rule set and refuses to emit any report containing a line that is
#     not in it.
#
# The gate deriving its expectation from configuration rather than from the
# generator's output is what makes the check independent: a generator change
# that started rendering applicant data would produce lines this module never
# authorised, and the report would be withheld.

from __future__ import annotations

import re

from src.services.service import Profile, RuleSet

# ── Fixed structure ───────────────────────────────────────────────────────────
REPORT_TITLE = "=== Underwriting Application Validation Report ==="
FIELD_SECTION = "--- Field Validation Results ---"
FLAG_SECTION = "--- Risk Flags ---"
REMEDIATION_SECTION = "--- Remediation Actions Required ---"
REPORT_FOOTER = "[UNDERWRITING VALIDATION COMPLETED]"
NO_FLAGS_LINE = "  (none)"
REFERENCE_ABSENT = "(not supplied)"

# ── Result labels ─────────────────────────────────────────────────────────────
RESULT_PASS = "pass"
RESULT_WARN = "warn"
RESULT_FAIL = "fail"
RESULT_LABELS: tuple[str, ...] = (RESULT_PASS, RESULT_WARN, RESULT_FAIL)

# ── Decisions ─────────────────────────────────────────────────────────────────
STATUS_PASS = "PASS"
STATUS_NIGO = "NIGO"
STATUS_REJECT = "REJECT"
OVERALL_STATUSES: tuple[str, ...] = (STATUS_PASS, STATUS_NIGO, STATUS_REJECT)

# ── Risk-flag taxonomy ────────────────────────────────────────────────────────
FLAG_INCOMPLETE_APPLICATION = "INCOMPLETE_APPLICATION"
FLAG_INVALID_POLICY_TYPE = "INVALID_POLICY_TYPE"
FLAG_ELIGIBILITY_FAIL = "ELIGIBILITY_FAIL"
FLAG_COVERAGE_BELOW_MINIMUM = "COVERAGE_BELOW_MINIMUM"
FLAG_COVERAGE_ABOVE_CEILING = "COVERAGE_ABOVE_CEILING"
FLAG_HIGH_RISK_INDICATOR = "HIGH_RISK_INDICATOR"
FLAG_FORMAT_ERROR = "FORMAT_ERROR"
FLAG_INVALID_NUMERIC = "INVALID_NUMERIC"

RISK_FLAGS: tuple[str, ...] = (
    FLAG_INCOMPLETE_APPLICATION,
    FLAG_INVALID_POLICY_TYPE,
    FLAG_ELIGIBILITY_FAIL,
    FLAG_COVERAGE_BELOW_MINIMUM,
    FLAG_COVERAGE_ABOVE_CEILING,
    FLAG_HIGH_RISK_INDICATOR,
    FLAG_FORMAT_ERROR,
    FLAG_INVALID_NUMERIC,
)

# Why a field received its label. Reason codes are internal: RiskFlagNode maps
# them onto the risk-flag taxonomy, and they never appear in the report.
REASON_OK = "ok"
REASON_MISSING = "missing"
REASON_NOT_FINITE = "not_finite"
REASON_BELOW_MINIMUM = "below_minimum"
REASON_ABOVE_CEILING = "above_ceiling"
REASON_ABOVE_THRESHOLD = "above_threshold"
REASON_OUT_OF_WINDOW = "out_of_window"
REASON_UNSUPPORTED_VALUE = "unsupported_value"
REASON_FORMAT_MISMATCH = "format_mismatch"
REASON_ABOVE_LIMIT = "above_limit"

REASON_CODES: tuple[str, ...] = (
    REASON_OK,
    REASON_MISSING,
    REASON_NOT_FINITE,
    REASON_BELOW_MINIMUM,
    REASON_ABOVE_CEILING,
    REASON_ABOVE_THRESHOLD,
    REASON_OUT_OF_WINDOW,
    REASON_UNSUPPORTED_VALUE,
    REASON_FORMAT_MISMATCH,
    REASON_ABOVE_LIMIT,
)

# Every application field the pipeline evaluates. Result-map keys are drawn from
# this set only — a caller cannot introduce a new key.
EVALUATED_FIELDS: tuple[str, ...] = (
    "policy_type",
    "coverage_amount",
    "age_at_application",
    "occupation_code",
    "premium_class",
    "policy_term_years",
    "beneficiary_count",
)


def _quoted_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def remediation_catalogue(rule_set: RuleSet, profile: Profile) -> dict[str, str]:
    """The remediation sentence permitted for each field under this profile.

    Threshold figures are rendered from the profile, so the sentences track the
    configured rules instead of restating a hard-coded copy of them.
    """
    return {
        "policy_type": (f"Provide a valid policy_type: one of {_quoted_list(rule_set.policy_types)}"),
        "coverage_amount": (
            f"Provide coverage_amount as a numeric value between "
            f"{profile.coverage_min:,} and {profile.coverage_max:,} (JPY)"
        ),
        "age_at_application": (
            f"Verify age_at_application: applicant must be between "
            f"{profile.age_min} and {profile.age_max} years of age"
        ),
        "occupation_code": (
            "Use a valid occupation_code format: one uppercase letter followed by three digits (e.g. A001)"
        ),
        "premium_class": (f"Provide a valid premium_class: one of {_quoted_list(rule_set.premium_classes)}"),
        "policy_term_years": (
            f"Provide policy_term_years as a whole number between "
            f"{profile.policy_term_years_min} and {profile.policy_term_years_max}"
        ),
        "beneficiary_count": (
            f"Provide beneficiary_count as a whole number between 0 and {profile.beneficiary_count_max}"
        ),
    }


def generic_remediation(field: str, result: str) -> str:
    """Fallback sentence for an evaluated field with no catalogue entry."""
    return f"Review field '{field}' (status: {result})"


def permitted_lines(rule_set: RuleSet, profile: Profile) -> frozenset[str]:
    """Every line the report is allowed to contain, for this rule set/profile.

    Lines carrying a validated caller identifier (the application reference and
    the profile name) are not enumerable and are matched by pattern instead —
    see REFERENCE_LINE_RE / PROFILE_LINE_RE.
    """
    lines: set[str] = {
        REPORT_TITLE,
        FIELD_SECTION,
        FLAG_SECTION,
        REMEDIATION_SECTION,
        REPORT_FOOTER,
        NO_FLAGS_LINE,
        "",
    }
    lines.update(f"Overall Status: {status}" for status in OVERALL_STATUSES)
    lines.update(f"  {field}: {label.upper()}" for field in EVALUATED_FIELDS for label in RESULT_LABELS)
    lines.update(f"  [{flag}]" for flag in RISK_FLAGS)

    catalogue = remediation_catalogue(rule_set, profile)
    sentences = set(catalogue.values())
    sentences.update(generic_remediation(field, label) for field in EVALUATED_FIELDS for label in RESULT_LABELS)
    # Remediation lines are numbered when rendered; the number is structural.
    lines.update(f"  {index}. {sentence}" for index in range(1, len(EVALUATED_FIELDS) + 1) for sentence in sentences)
    return frozenset(lines)


# Identifier-bearing lines. The identifier itself was validated as inert before
# it reached the report, and the pattern here re-checks that at the boundary.
REFERENCE_LINE_RE = re.compile(rf"^Application Reference: (?:[a-z0-9_]{{1,32}}|{re.escape(REFERENCE_ABSENT)})$")
PROFILE_LINE_RE = re.compile(r"^Rule Profile: [a-z0-9_]{1,32}$")
