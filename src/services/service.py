"""AgentCore Platform v1.0 — INS-C2-006 underwriting rule-set service."""

# Loads and validates config/rules.yaml — the single source of the underwriting
# rules the pipeline applies. Nodes call get_rule_set(); nothing else reads the
# file, and the code holds no second copy of the thresholds.
#
# Validation on load is deliberate. The thresholds decide whether an
# application is accepted, so a malformed value must stop the request rather
# than quietly reverting to a built-in number: every threshold goes through the
# same finite+bounded parser the caller inputs use, the orderings are checked,
# and a violation raises RuleSetError. Absence of the file is different from
# corruption of it — with no file at all the built-in baseline below applies.
#
# Service layer rules: domain data access only. No routing, no credentials.

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from src.schemas.state import finite_in_range, is_inert_identifier

_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "rules.yaml"

# Outer bounds for the declared thresholds. A rule file may narrow these; it may
# not exceed them.
_COVERAGE_CEILING = 1e12
_AGE_CEILING = 130
_TERM_YEARS_CEILING = 120
_BENEFICIARY_CEILING = 100

# Numeric threshold keys every profile must declare, with their outer bounds.
_PROFILE_NUMERIC_KEYS: dict[str, tuple[float, float]] = {
    "coverage_min": (0.0, _COVERAGE_CEILING),
    "coverage_warn": (0.0, _COVERAGE_CEILING),
    "coverage_max": (0.0, _COVERAGE_CEILING),
    "age_min": (0.0, _AGE_CEILING),
    "age_max": (0.0, _AGE_CEILING),
    "policy_term_years_min": (0.0, _TERM_YEARS_CEILING),
    "policy_term_years_max": (0.0, _TERM_YEARS_CEILING),
    "beneficiary_count_max": (0.0, _BENEFICIARY_CEILING),
}

# Baseline used only when config/rules.yaml is absent.
_BASELINE: dict[str, Any] = {
    "version": 1,
    "required_fields": ["policy_type", "coverage_amount", "age_at_application"],
    "policy_types": ["term_life", "whole_life", "endowment"],
    "premium_classes": ["standard", "rated", "declined"],
    "occupation_code_pattern": r"^[A-Z][0-9]{3}$",
    "default_profile": "standard",
    "profiles": {
        "standard": {
            "coverage_min": 1_000_000,
            "coverage_warn": 50_000_000,
            "coverage_max": 100_000_000,
            "age_min": 18,
            "age_max": 80,
            "policy_term_years_min": 1,
            "policy_term_years_max": 60,
            "beneficiary_count_max": 10,
        }
    },
}


class RuleSetError(ValueError):
    """The rule file is present but does not describe a usable rule set."""


@dataclass(frozen=True)
class Profile:
    """One underwriting threshold profile."""

    name: str
    coverage_min: int
    coverage_warn: int
    coverage_max: int
    age_min: int
    age_max: int
    policy_term_years_min: int
    policy_term_years_max: int
    beneficiary_count_max: int


@dataclass(frozen=True)
class RuleSet:
    """The validated underwriting rule set."""

    required_fields: frozenset[str]
    policy_types: tuple[str, ...]
    premium_classes: tuple[str, ...]
    occupation_code_pattern: re.Pattern[str]
    default_profile: str
    profiles: dict[str, Profile]

    def profile(self, name: str | None) -> Profile:
        """Return the named profile, or the default when name is empty/unknown.

        An unknown name is not silently accepted: callers validate the name
        against profile_names() before reaching this method.
        """
        if name and name in self.profiles:
            return self.profiles[name]
        return self.profiles[self.default_profile]

    def profile_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.profiles))


def _whole_number(raw: Any, lo: float, hi: float, where: str) -> int:
    parsed = finite_in_range(raw, lo, hi)
    if parsed is None or parsed != int(parsed):
        raise RuleSetError(f"{where} must be a whole number between {int(lo)} and {int(hi)}")
    return int(parsed)


def _identifier_list(raw: Any, where: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise RuleSetError(f"{where} must be a non-empty list")
    values: list[str] = []
    for item in raw:
        if not is_inert_identifier(item):
            raise RuleSetError(f"{where} entries must be inert identifiers")
        values.append(item)
    return tuple(values)


def _build_profile(name: str, raw: Any) -> Profile:
    if not is_inert_identifier(name):
        raise RuleSetError("profile names must be inert identifiers")
    if not isinstance(raw, dict):
        raise RuleSetError(f"profile '{name}' must be a mapping")
    values: dict[str, int] = {}
    for key, (lo, hi) in _PROFILE_NUMERIC_KEYS.items():
        if key not in raw:
            raise RuleSetError(f"profile '{name}' is missing '{key}'")
        values[key] = _whole_number(raw[key], lo, hi, f"profile '{name}' key '{key}'")
    if not values["coverage_min"] <= values["coverage_warn"] <= values["coverage_max"]:
        raise RuleSetError(f"profile '{name}' coverage thresholds must be ordered min <= warn <= max")
    if values["age_min"] > values["age_max"]:
        raise RuleSetError(f"profile '{name}' age_min must not exceed age_max")
    if values["policy_term_years_min"] > values["policy_term_years_max"]:
        raise RuleSetError(f"profile '{name}' policy_term_years_min must not exceed policy_term_years_max")
    return Profile(name=name, **values)


def build_rule_set(raw: Any) -> RuleSet:
    """Validate a decoded rule mapping and build the RuleSet.

    Raises RuleSetError on anything that would leave a threshold undefined,
    unordered, or outside its outer bound.
    """
    if not isinstance(raw, dict):
        raise RuleSetError("rule set must be a mapping")

    required_fields = _identifier_list(raw.get("required_fields"), "required_fields")
    policy_types = _identifier_list(raw.get("policy_types"), "policy_types")
    premium_classes = _identifier_list(raw.get("premium_classes"), "premium_classes")

    pattern_text = raw.get("occupation_code_pattern")
    if not isinstance(pattern_text, str) or not pattern_text:
        raise RuleSetError("occupation_code_pattern must be a non-empty string")
    try:
        occupation_pattern = re.compile(pattern_text)
    except re.error as exc:
        raise RuleSetError(f"occupation_code_pattern is not a valid pattern: {exc}") from exc

    raw_profiles = raw.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise RuleSetError("profiles must be a non-empty mapping")
    profiles = {name: _build_profile(name, body) for name, body in raw_profiles.items()}

    default_profile = raw.get("default_profile")
    if not is_inert_identifier(default_profile) or default_profile not in profiles:
        raise RuleSetError("default_profile must name one of the declared profiles")

    return RuleSet(
        required_fields=frozenset(required_fields),
        policy_types=policy_types,
        premium_classes=premium_classes,
        occupation_code_pattern=occupation_pattern,
        default_profile=str(default_profile),
        profiles=profiles,
    )


def load_rule_set(path: Path | None = None) -> RuleSet:
    """Read and validate the rule file; fall back to the baseline when absent."""
    rules_path = path or _RULES_PATH
    if not rules_path.exists():
        return build_rule_set(_BASELINE)
    with open(rules_path, encoding="utf-8") as handle:
        decoded = yaml.safe_load(handle)
    return build_rule_set(decoded)


@lru_cache(maxsize=1)
def get_rule_set() -> RuleSet:
    """Cached accessor — the rule file is read once per process."""
    return load_rule_set()
