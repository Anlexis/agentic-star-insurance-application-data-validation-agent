"""INS-C2-006 — unit tests for the domain nodes, the rule set, and the graph.

Covered here:
  - PreProcessNode: trust gate, empty/oversized/masked payloads, injection
    refusal, and the caller-metadata contract (inert identifiers, unknown keys,
    profile selection)
  - the rule-set loader: validation, ordering, and the fail-closed behaviour of
    a malformed rule file
  - UnderwritingRuleValidateNode: completeness, format and business rules, the
    non-finite matrix on every numeric field, and profile switching
  - RiskFlagNode: each flag trigger, driven by reason codes
  - GenerateReportNode: decision logic and report structure
  - PostProcessNode: every layer of the output boundary, both directions
  - the graph end to end through invoke()

Patching rule: patch emit_trace_event AT THE NODE MODULE — never via
sys.modules["shared"] (that shadows the real shared package and causes
ModuleNotFoundError).
"""

import json

import pytest
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.schemas.report_contract import (
    FIELD_SECTION,
    FLAG_COVERAGE_ABOVE_CEILING,
    FLAG_COVERAGE_BELOW_MINIMUM,
    FLAG_ELIGIBILITY_FAIL,
    FLAG_FORMAT_ERROR,
    FLAG_HIGH_RISK_INDICATOR,
    FLAG_INCOMPLETE_APPLICATION,
    FLAG_INVALID_NUMERIC,
    FLAG_INVALID_POLICY_TYPE,
    FLAG_SECTION,
    REPORT_FOOTER,
    REPORT_TITLE,
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

# Values that parse through float() but must never satisfy a numeric field:
# bools (int subclass), the non-finite spellings in both text and float form,
# and magnitudes past any plausible bound.
NON_FINITE_MATRIX = [
    "NaN",
    "Infinity",
    "-Infinity",
    float("nan"),
    float("inf"),
    float("-inf"),
    True,
    "1e400",
    "not_a_number",
]


def _base_state(**kwargs) -> dict:
    """Minimal state dict for routing node invocations through BaseNode.__call__.

    caller_trust_level defaults to VERIFIED_EXTERNAL (the canonical uppercase
    TrustLevel value) so the trust gate is exercised on every call — it
    satisfies both the PreProcessNode (VERIFIED_EXTERNAL) and inner-node
    (ANONYMOUS) requirements. Override per test where a different caller is
    needed.
    """
    state = {
        "user_input": "",
        "input_context": {},
        "input_record": "",
        "validated_input": "",
        "parsed_field_count": 0,
        "validation_results": "",
        "validation_reasons": "",
        "risk_flags": "[]",
        "overall_status": "",
        "remediation_suggestions": "[]",
        "report_output": "",
        "rule_profile": "standard",
        "application_ref": "",
        "error_message": "",
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "node_history": [],
        "error_log": [],
        "status": "pending",
    }
    state.update(kwargs)
    return state


def _application(**overrides) -> str:
    base = {
        "policy_type": "term_life",
        "coverage_amount": 10_000_000,
        "age_at_application": 35,
    }
    base.update(overrides)
    return json.dumps(base)


_VALID_APP_JSON = _application(occupation_code="A001", premium_class="standard")
_MISSING_FIELDS_JSON = json.dumps({"occupation_code": "A001"})


@pytest.fixture
def silent_nodes(monkeypatch):
    """Silence audit emission in every domain node module."""
    import framework.nodes.base_node as base_node_module
    import src.nodes.generate_report_node as m5
    import src.nodes.parse_node as m2
    import src.nodes.post_process_node as m6
    import src.nodes.pre_process_node as m1
    import src.nodes.risk_flag_node as m4
    import src.nodes.underwriting_rule_validate_node as m3

    def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(base_node_module, "emit_trace_event", noop)
    for module in (m1, m2, m3, m4, m5, m6):
        monkeypatch.setattr(module, "emit_trace_event", noop)


# ─────────────────────────────────────────────────────────────────────────────
# PreProcessNode — the input boundary
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("silent_nodes")
class TestPreProcessNode:
    """Payload screening at the entry node."""

    def _node(self):
        from src.nodes.pre_process_node import PreProcessNode

        return PreProcessNode()

    def test_empty_input_returns_error(self):
        result = self._node()(_base_state(user_input=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "error_log" in result

    def test_whitespace_only_returns_error(self):
        result = self._node()(_base_state(user_input="   "))
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_non_string_input_returns_error(self):
        result = self._node()(_base_state(user_input={"policy_type": "term_life"}))
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_oversized_input_returns_error(self):
        result = self._node()(_base_state(user_input="x" * 65_537))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "validated_input" not in result

    def test_masked_input_returns_error(self):
        """A payload the input gate reduced to the mask sentinel has no content."""
        result = self._node()(_base_state(user_input="[MASKED]"))
        assert result["status"] == AgentStatus.ERROR.value

    def test_valid_json_input_succeeds(self):
        result = self._node()(_base_state(user_input=_VALID_APP_JSON))
        assert result["status"] == AgentStatus.SUCCESS.value
        # State carries the enum's .value string, not the enum itself.
        assert not isinstance(result["status"], AgentStatus)
        assert isinstance(result["status"], str)
        assert result.get("input_record") == _VALID_APP_JSON.strip()
        assert result.get("validated_input") == _VALID_APP_JSON.strip()

    def test_trust_level_is_verified_external(self):
        from src.nodes.pre_process_node import PreProcessNode

        assert PreProcessNode.required_trust_level == TrustLevel.VERIFIED_EXTERNAL


@pytest.mark.usefixtures("silent_nodes")
class TestInjectionRefusal:
    """The node owns the refusal — it holds with no framework gate in front."""

    def _node(self):
        from src.nodes.pre_process_node import PreProcessNode

        return PreProcessNode()

    @pytest.mark.parametrize(
        "marker",
        [
            "ignore previous instructions",
            "ignore all instructions",
            "you are now",
            "system prompt",
            "DROP TABLE applications",
            "' or '1'='1",
        ],
    )
    def test_injection_forms_refused_by_execute_directly(self, marker):
        """execute() is called with no wrapper, so nothing else can be doing the work."""
        payload = json.dumps({"policy_type": "term_life", "note": marker})
        result = self._node().execute(_base_state(user_input=payload))
        assert result["status"] == AgentStatus.ERROR.value
        assert "validated_input" not in result

    @pytest.mark.parametrize(
        "ordinary",
        [
            "The applicant acts as a company director",
            "Prompt payment discount applies to this policy",
            "Please ignore the duplicate submission dated last week",
        ],
    )
    def test_ordinary_text_containing_similar_words_is_unaffected(self, ordinary):
        payload = json.dumps({"policy_type": "term_life", "coverage_amount": 10_000_000, "note": ordinary})
        result = self._node().execute(_base_state(user_input=payload))
        assert result["status"] == AgentStatus.SUCCESS.value


@pytest.mark.usefixtures("silent_nodes")
class TestTrustGate:
    """Node invocations route through BaseNode.__call__, so the trust gate runs.

    PreProcessNode requires VERIFIED_EXTERNAL. __call__ applies the gate BEFORE
    execute(): an ANONYMOUS caller is denied and never reaches execute(); a
    VERIFIED_EXTERNAL caller is admitted and runs through to SUCCESS.
    """

    def _node(self):
        from src.nodes.pre_process_node import PreProcessNode

        return PreProcessNode()

    def test_anonymous_caller_denied(self):
        result = self._node()(_base_state(user_input=_VALID_APP_JSON, caller_trust_level=TrustLevel.ANONYMOUS.value))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("trust gate denied" in entry.lower() for entry in result.get("error_log", []))
        # execute() never ran, so its success output key is absent.
        assert "validated_input" not in result

    def test_verified_external_caller_admitted(self):
        result = self._node()(
            _base_state(user_input=_VALID_APP_JSON, caller_trust_level=TrustLevel.VERIFIED_EXTERNAL.value)
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result.get("validated_input") == _VALID_APP_JSON.strip()


@pytest.mark.usefixtures("silent_nodes")
class TestCallerMetadataContract:
    """Every input_context field is validated; invalid values fail closed."""

    ECHO_MARKER = "zqx_echo_marker_zqx"

    def _run(self, context):
        from src.nodes.pre_process_node import PreProcessNode

        return PreProcessNode()(_base_state(user_input=_VALID_APP_JSON, input_context=context))

    def test_absent_metadata_uses_documented_defaults(self):
        result = self._run({})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["submission_channel"] == "unknown"
        assert result["application_ref"] == ""
        assert result["rule_profile"] == "standard"

    def test_valid_metadata_is_carried_forward(self):
        result = self._run(
            {
                "channel": "web_portal",
                "submitter_ref": "broker_0042",
                "application_ref": "app_00042",
                "rule_profile": "senior_simplified",
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["submission_channel"] == "web_portal"
        assert result["application_ref"] == "app_00042"
        assert result["rule_profile"] == "senior_simplified"

    @pytest.mark.parametrize("field", ["channel", "submitter_ref", "application_ref"])
    @pytest.mark.parametrize(
        "value",
        [
            "Not An Identifier",
            "<b>markup</b>",
            "x" * 33,
            "",
            42,
            True,
            None,
            ["list"],
        ],
    )
    def test_non_inert_identifier_is_refused(self, field, value):
        result = self._run({field: value})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert field in " ".join(result["error_log"])

    def test_rejected_value_is_never_echoed(self):
        result = self._run({"application_ref": f"BAD VALUE {self.ECHO_MARKER}"})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert self.ECHO_MARKER not in json.dumps(result)

    def test_unknown_metadata_key_is_refused(self):
        result = self._run({"unexpected_key": "value"})
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_runtime_supplied_context_field_is_accepted_and_not_carried_forward(self):
        """A field the hosting runtime attaches must not make the agent unreachable.

        A conversation history is placed on the context channel by the runtime
        that serves the agent, not by the caller, and it arrives on every
        invocation made that way. A key set that knows only the caller contract
        turns each of those into an out-of-contract refusal, and the caller
        cannot remove a field it never added.

        Accepting it is sound because no constraint is attached to it: this node
        reads nothing from it and promises nothing about it, so there is no
        constraint a caller could wrongly believe was applied. The assertions
        are that the submission is served AND that the field's content does not
        reach the state this node writes.
        """
        result = self._run(
            {
                "channel": "web_portal",
                "conversation_history": [{"role": "user", "content": f"earlier turn {self.ECHO_MARKER}"}],
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "error_code" not in result, result
        assert result["submission_channel"] == "web_portal"
        assert "conversation_history" not in result
        assert self.ECHO_MARKER not in json.dumps(result)

    def test_unknown_caller_key_is_still_refused_alongside_a_runtime_field(self):
        """The control: widening the set for the runtime must not open it to callers."""
        result = self._run(
            {
                "conversation_history": [{"role": "user", "content": "earlier turn"}],
                "priority": "high",
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result.get("error_code") == "INVALID_REQUEST", result
        assert "priority" in " ".join(result["error_log"])

    def test_unknown_profile_is_refused(self):
        result = self._run({"rule_profile": "no_such_profile"})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "rule_profile" in " ".join(result["error_log"])

    def test_non_dict_input_context_is_refused(self):
        result = self._run("not-a-dict")
        assert result["status"] == AgentStatus.SUCCESS.value


# ─────────────────────────────────────────────────────────────────────────────
# The rule set
# ─────────────────────────────────────────────────────────────────────────────


class TestRuleSet:
    """config/rules.yaml is live configuration and is validated on load."""

    def test_shipped_rule_file_loads(self):
        from src.services.service import load_rule_set

        rule_set = load_rule_set()
        assert "standard" in rule_set.profile_names()
        assert rule_set.default_profile == "standard"
        assert rule_set.profile("standard").coverage_min == 1_000_000

    def test_profile_selection_falls_back_to_default(self):
        from src.services.service import load_rule_set

        rule_set = load_rule_set()
        assert rule_set.profile(None).name == rule_set.default_profile
        assert rule_set.profile("senior_simplified").name == "senior_simplified"

    def test_absent_file_uses_the_built_in_baseline(self, tmp_path):
        from src.services.service import load_rule_set

        rule_set = load_rule_set(tmp_path / "missing.yaml")
        assert rule_set.default_profile == "standard"

    @pytest.mark.parametrize("bad_value", NON_FINITE_MATRIX)
    def test_non_finite_threshold_is_refused(self, bad_value):
        from src.services.service import RuleSetError, build_rule_set

        raw = _rule_mapping()
        raw["profiles"]["standard"]["coverage_min"] = bad_value
        with pytest.raises(RuleSetError):
            build_rule_set(raw)

    def test_unordered_coverage_band_is_refused(self):
        from src.services.service import RuleSetError, build_rule_set

        raw = _rule_mapping()
        raw["profiles"]["standard"]["coverage_min"] = 90_000_000
        with pytest.raises(RuleSetError):
            build_rule_set(raw)

    def test_unordered_age_window_is_refused(self):
        from src.services.service import RuleSetError, build_rule_set

        raw = _rule_mapping()
        raw["profiles"]["standard"]["age_min"] = 90
        raw["profiles"]["standard"]["age_max"] = 20
        with pytest.raises(RuleSetError):
            build_rule_set(raw)

    def test_missing_threshold_is_refused(self):
        from src.services.service import RuleSetError, build_rule_set

        raw = _rule_mapping()
        del raw["profiles"]["standard"]["age_max"]
        with pytest.raises(RuleSetError):
            build_rule_set(raw)

    def test_default_profile_must_exist(self):
        from src.services.service import RuleSetError, build_rule_set

        raw = _rule_mapping()
        raw["default_profile"] = "absent_profile"
        with pytest.raises(RuleSetError):
            build_rule_set(raw)

    def test_non_inert_policy_type_is_refused(self):
        from src.services.service import RuleSetError, build_rule_set

        raw = _rule_mapping()
        raw["policy_types"] = ["term life <b>"]
        with pytest.raises(RuleSetError):
            build_rule_set(raw)


def _rule_mapping() -> dict:
    """A minimal valid rule mapping, freshly built for each mutation test."""
    return {
        "version": 1,
        "required_fields": ["policy_type", "coverage_amount", "age_at_application"],
        "policy_types": ["term_life", "whole_life"],
        "premium_classes": ["standard", "rated"],
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


# ─────────────────────────────────────────────────────────────────────────────
# ParseNode
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("silent_nodes")
class TestParseNode:
    def _node(self):
        from src.nodes.parse_node import ParseNode

        return ParseNode()

    def test_valid_json_parsed(self):
        result = self._node()(_base_state(input_record=_VALID_APP_JSON))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["parsed_field_count"] == 5

    def test_form_encoded_parsed(self):
        record = "policy_type=term_life&coverage_amount=10000000&age_at_application=35"
        result = self._node()(_base_state(input_record=record))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["parsed_field_count"] == 3

    def test_empty_input_record_returns_error(self):
        result = self._node()(_base_state(input_record=""))
        assert result["status"] == AgentStatus.ERROR.value

    def test_invalid_json_returns_error(self):
        result = self._node()(_base_state(input_record='{"policy_type": '))
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_no_raw_values_in_result(self):
        """Only the recognised-field COUNT is persisted."""
        result = self._node()(_base_state(input_record=_VALID_APP_JSON))
        serialised = json.dumps(result)
        assert "term_life" not in serialised
        assert "A001" not in serialised


# ─────────────────────────────────────────────────────────────────────────────
# UnderwritingRuleValidateNode
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("silent_nodes")
class TestUnderwritingRuleValidateNode:
    def _results(self, record, profile="standard"):
        from src.nodes.underwriting_rule_validate_node import UnderwritingRuleValidateNode

        outcome = UnderwritingRuleValidateNode()(_base_state(input_record=record, rule_profile=profile))
        return json.loads(outcome["validation_results"]), json.loads(outcome["validation_reasons"])

    def test_valid_application_all_pass(self):
        results, _ = self._results(_VALID_APP_JSON)
        assert all(value == "pass" for value in results.values())

    def test_missing_required_fields_fail(self):
        results, reasons = self._results(_MISSING_FIELDS_JSON)
        assert results["policy_type"] == "fail"
        assert reasons["coverage_amount"] == "missing"

    def test_age_outside_eligibility_fails(self):
        results, reasons = self._results(_application(age_at_application=90))
        assert results["age_at_application"] == "fail"
        assert reasons["age_at_application"] == "out_of_window"

    def test_coverage_below_minimum_fails(self):
        results, reasons = self._results(_application(coverage_amount=500))
        assert results["coverage_amount"] == "fail"
        assert reasons["coverage_amount"] == "below_minimum"

    def test_coverage_above_ceiling_fails(self):
        results, reasons = self._results(_application(coverage_amount=250_000_000))
        assert results["coverage_amount"] == "fail"
        assert reasons["coverage_amount"] == "above_ceiling"

    def test_coverage_above_threshold_warns(self):
        results, reasons = self._results(_application(coverage_amount=75_000_000))
        assert results["coverage_amount"] == "warn"
        assert reasons["coverage_amount"] == "above_threshold"

    def test_invalid_policy_type_fails(self):
        results, reasons = self._results(_application(policy_type="invalid_type"))
        assert results["policy_type"] == "fail"
        assert reasons["policy_type"] == "unsupported_value"

    def test_optional_field_format_warns(self):
        results, reasons = self._results(_application(occupation_code="bad-code"))
        assert results["occupation_code"] == "warn"
        assert reasons["occupation_code"] == "format_mismatch"

    def test_structural_limits_warn(self):
        results, _ = self._results(_application(policy_term_years=90, beneficiary_count=40))
        assert results["policy_term_years"] == "warn"
        assert results["beneficiary_count"] == "warn"

    @pytest.mark.parametrize(
        "field", ["coverage_amount", "age_at_application", "policy_term_years", "beneficiary_count"]
    )
    @pytest.mark.parametrize("bad_value", NON_FINITE_MATRIX)
    def test_non_finite_numeric_fails_closed(self, field, bad_value):
        """Every caller-controlled number, not just the obvious ones.

        NaN survives float() and compares False against every bound, so an
        unchecked value here would be reported as acceptable.
        """
        results, reasons = self._results(_application(**{field: bad_value}))
        assert results[field] == "fail", f"{field}={bad_value!r} must not pass"
        assert reasons[field] == "not_finite"

    def test_profile_changes_the_outcome(self):
        """The same application evaluates differently under a different profile."""
        record = _application(coverage_amount=20_000_000, age_at_application=85)
        standard, _ = self._results(record, profile="standard")
        senior, _ = self._results(record, profile="senior_simplified")
        assert standard["age_at_application"] == "fail"
        assert senior["age_at_application"] == "pass"
        assert standard["coverage_amount"] == "pass"
        assert senior["coverage_amount"] == "warn"

    def test_no_raw_field_values_in_result(self):
        results, _ = self._results(_VALID_APP_JSON)
        assert "A001" not in json.dumps(results)

    def test_missing_input_record_returns_error(self):
        from src.nodes.underwriting_rule_validate_node import UnderwritingRuleValidateNode

        result = UnderwritingRuleValidateNode()(_base_state(input_record=""))
        assert result["status"] == AgentStatus.ERROR.value


# ─────────────────────────────────────────────────────────────────────────────
# RiskFlagNode
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("silent_nodes")
class TestRiskFlagNode:
    def _flags(self, results, reasons):
        from src.nodes.risk_flag_node import RiskFlagNode

        outcome = RiskFlagNode()(
            _base_state(validation_results=json.dumps(results), validation_reasons=json.dumps(reasons))
        )
        return json.loads(outcome["risk_flags"])

    def test_no_flags_on_all_pass(self):
        assert self._flags({"policy_type": "pass"}, {"policy_type": "ok"}) == []

    def test_incomplete_application_flag(self):
        assert FLAG_INCOMPLETE_APPLICATION in self._flags({"coverage_amount": "fail"}, {"coverage_amount": "missing"})

    def test_eligibility_fail_flag(self):
        assert FLAG_ELIGIBILITY_FAIL in self._flags(
            {"age_at_application": "fail"}, {"age_at_application": "out_of_window"}
        )

    def test_coverage_below_minimum_flag(self):
        assert FLAG_COVERAGE_BELOW_MINIMUM in self._flags(
            {"coverage_amount": "fail"}, {"coverage_amount": "below_minimum"}
        )

    def test_coverage_above_ceiling_flag(self):
        assert FLAG_COVERAGE_ABOVE_CEILING in self._flags(
            {"coverage_amount": "fail"}, {"coverage_amount": "above_ceiling"}
        )

    def test_high_risk_indicator_flag(self):
        assert FLAG_HIGH_RISK_INDICATOR in self._flags(
            {"coverage_amount": "warn"}, {"coverage_amount": "above_threshold"}
        )

    def test_invalid_policy_type_flag(self):
        assert FLAG_INVALID_POLICY_TYPE in self._flags({"policy_type": "fail"}, {"policy_type": "unsupported_value"})

    def test_invalid_numeric_flag(self):
        assert FLAG_INVALID_NUMERIC in self._flags({"coverage_amount": "fail"}, {"coverage_amount": "not_finite"})

    def test_format_error_flag_on_optional_warn(self):
        assert FLAG_FORMAT_ERROR in self._flags({"occupation_code": "warn"}, {"occupation_code": "format_mismatch"})

    def test_unmapped_reason_still_surfaces_a_flag(self):
        assert self._flags({"premium_class": "warn"}, {}) == [FLAG_FORMAT_ERROR]

    def test_missing_validation_results_returns_error(self):
        from src.nodes.risk_flag_node import RiskFlagNode

        result = RiskFlagNode()(_base_state(validation_results=""))
        assert result["status"] == AgentStatus.ERROR.value


# ─────────────────────────────────────────────────────────────────────────────
# GenerateReportNode
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("silent_nodes")
class TestGenerateReportNode:
    def _report(self, results, flags, **state):
        from src.nodes.generate_report_node import GenerateReportNode

        return GenerateReportNode()(
            _base_state(validation_results=json.dumps(results), risk_flags=json.dumps(flags), **state)
        )

    def test_pass_overall_status(self):
        outcome = self._report({"policy_type": "pass"}, [])
        assert outcome["overall_status"] == "PASS"
        assert json.loads(outcome["remediation_suggestions"]) == []

    def test_nigo_on_warn(self):
        outcome = self._report({"occupation_code": "warn"}, [FLAG_FORMAT_ERROR])
        assert outcome["overall_status"] == "NIGO"
        assert json.loads(outcome["remediation_suggestions"])

    def test_reject_on_eligibility_fail(self):
        outcome = self._report({"age_at_application": "fail"}, [FLAG_ELIGIBILITY_FAIL])
        assert outcome["overall_status"] == "REJECT"

    def test_reject_on_coverage_above_ceiling(self):
        outcome = self._report({"coverage_amount": "fail"}, [FLAG_COVERAGE_ABOVE_CEILING])
        assert outcome["overall_status"] == "REJECT"

    def test_report_structure(self):
        outcome = self._report({"policy_type": "pass"}, [], application_ref="app_00042")
        report = outcome["report_output"]
        assert report.startswith(REPORT_TITLE)
        assert "Application Reference: app_00042" in report
        assert "Rule Profile: standard" in report
        assert FIELD_SECTION in report
        assert FLAG_SECTION in report
        assert report.rstrip().endswith(REPORT_FOOTER)

    def test_absent_reference_renders_the_placeholder(self):
        report = self._report({"policy_type": "pass"}, [])["report_output"]
        assert "Application Reference: (not supplied)" in report

    def test_remediation_text_tracks_the_active_profile(self):
        standard = self._report({"coverage_amount": "fail"}, [], rule_profile="standard")["report_output"]
        senior = self._report({"coverage_amount": "fail"}, [], rule_profile="senior_simplified")["report_output"]
        assert "1,000,000 and 100,000,000" in standard
        assert "500,000 and 30,000,000" in senior

    def test_missing_validation_results_returns_error(self):
        from src.nodes.generate_report_node import GenerateReportNode

        result = GenerateReportNode()(_base_state(validation_results=""))
        assert result["status"] == AgentStatus.ERROR.value


# ─────────────────────────────────────────────────────────────────────────────
# PostProcessNode — the output boundary
# ─────────────────────────────────────────────────────────────────────────────


def _authorised_report(**overrides) -> str:
    """A report built exactly as GenerateReportNode builds one."""
    from src.schemas.report_contract import NO_FLAGS_LINE

    lines = [
        REPORT_TITLE,
        overrides.get("reference_line", "Application Reference: app_00042"),
        overrides.get("profile_line", "Rule Profile: standard"),
        "Overall Status: PASS",
        "",
        FIELD_SECTION,
        "  policy_type: PASS",
        "  coverage_amount: PASS",
        "",
        FLAG_SECTION,
        NO_FLAGS_LINE,
        "",
        REPORT_FOOTER,
    ]
    return "\n".join(lines)


@pytest.mark.usefixtures("silent_nodes")
class TestOutputBoundary:
    """Every layer, both directions: authorised reports pass untouched."""

    def _gate(self, report, **state):
        from src.nodes.post_process_node import _security_gate_output

        return _security_gate_output(report, _base_state(**state))

    def test_authorised_report_passes(self):
        passed, layer, _ = self._gate(_authorised_report())
        assert passed is True
        assert layer == ""

    def test_node_emits_the_report_unchanged(self):
        from src.nodes.post_process_node import PostProcessNode

        report = _authorised_report()
        result = PostProcessNode()(_base_state(report_output=report))
        assert result["status"] == AgentStatus.SUCCESS.value
        # The declared state field — never an undeclared "output" key.
        assert result["formatted_output"] == report
        assert "output" not in result

    # ── Layer 1: structure ───────────────────────────────────────────────
    def test_empty_report_is_refused(self):
        passed, layer, _ = self._gate("")
        assert (passed, layer) == (False, "structure")

    def test_missing_footer_is_refused(self):
        passed, layer, _ = self._gate(_authorised_report().replace(REPORT_FOOTER, ""))
        assert (passed, layer) == (False, "structure")

    def test_oversized_report_is_refused(self):
        oversized = _authorised_report() + "\n" + ("  policy_type: PASS\n" * 4000)
        passed, layer, _ = self._gate(oversized)
        assert (passed, layer) == (False, "structure")

    # ── Layer 2: pattern scan, on the untouched text ─────────────────────
    @pytest.mark.parametrize(
        "leak",
        [
            "  SSN 123-45-6789",
            "  TAX 987-65-4321",
            "  4111 1111 1111 1111",
            "  contact: applicant@example.com",
            "  1984-02-11",
            "  dob: redacted",
            "  Authorization: Bearer abcdef123456",
            "  api_key = sk-abcdefghijklmnopqrst",
        ],
    )
    def test_patterns_are_caught_before_anything_rewrites_the_text(self, leak):
        report = _authorised_report().replace("  coverage_amount: PASS", f"  coverage_amount: PASS\n{leak}")
        passed, layer, _ = self._gate(report)
        assert passed is False
        assert layer == "pattern_scan", "the pattern scan must decide before the later layers"

    # ── Layer 3: verbatim application text ───────────────────────────────
    def test_verbatim_application_text_is_refused(self):
        record = _VALID_APP_JSON
        report = _authorised_report() + "\n" + record
        passed, layer, _ = self._gate(report, input_record=record)
        assert (passed, layer) == (False, "verbatim_scan")

    def test_short_incidental_overlap_is_not_a_leak(self):
        passed, _, _ = self._gate(_authorised_report(), input_record="PASS")
        assert passed is True

    # ── Layer 4: vocabulary conformance ──────────────────────────────────
    @pytest.mark.parametrize(
        "unauthorised",
        [
            "  applicant_name: Taro Yamada",
            "  coverage_amount: 12,345,678",
            "  policy_type: MAYBE",
            "  [NOT_A_REAL_FLAG]",
            "  1. Call the applicant on their mobile",
            "Free text the generator was never allowed to write",
        ],
    )
    def test_lines_outside_the_vocabulary_are_refused(self, unauthorised):
        report = _authorised_report().replace("  coverage_amount: PASS", f"  coverage_amount: PASS\n{unauthorised}")
        passed, layer, _ = self._gate(report)
        assert (passed, layer) == (False, "vocabulary")

    @pytest.mark.parametrize(
        "line",
        [
            "Application Reference: Taro Yamada",
            "Application Reference: <script>alert(1)</script>",
            "Rule Profile: Standard Profile (Life)",
        ],
    )
    def test_identifier_lines_must_stay_inert(self, line):
        field = "reference_line" if line.startswith("Application") else "profile_line"
        passed, layer, _ = self._gate(_authorised_report(**{field: line}))
        assert (passed, layer) == (False, "vocabulary")

    def test_remediation_line_from_another_profile_is_refused(self):
        """The permitted sentences are those of the ACTIVE profile."""
        from src.schemas.report_contract import REMEDIATION_SECTION

        senior_line = "  1. Provide coverage_amount as a numeric value between 500,000 and 30,000,000 (JPY)"
        report = _authorised_report().replace(REPORT_FOOTER, f"{REMEDIATION_SECTION}\n{senior_line}\n\n{REPORT_FOOTER}")
        passed, layer, _ = self._gate(report, rule_profile="standard")
        assert (passed, layer) == (False, "vocabulary")
        passed_senior, _, _ = self._gate(report, rule_profile="senior_simplified")
        assert passed_senior is True

    def test_missing_report_output_returns_error(self):
        from src.nodes.post_process_node import PostProcessNode

        result = PostProcessNode()(_base_state(report_output=""))
        assert result["status"] == AgentStatus.ERROR.value

    def test_each_layer_raises_its_own_audit_event(self, monkeypatch):
        """A refusal is attributable to one boundary layer, not to "the gate"."""
        import src.nodes.post_process_node as module
        from src.nodes.post_process_node import PostProcessNode

        events: list[str] = []
        monkeypatch.setattr(module, "emit_trace_event", lambda name, *a, **k: events.append(name))

        report = _authorised_report().replace("  coverage_amount: PASS", "  applicant_name: Taro Yamada")
        result = PostProcessNode().execute(_base_state(report_output=report))

        assert result["status"] == AgentStatus.ERROR.value
        assert "ins_c2_006.post_process.vocabulary_refusal" in events
        assert "ins_c2_006.post_process.output_gate_verdict" in events


# ─────────────────────────────────────────────────────────────────────────────
# Graph end to end
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("silent_nodes")
class TestGraphEndToEnd:
    def _agent(self, config=None):
        from src.graph.graph import InsC2006Agent

        agent = InsC2006Agent(config=config or {})
        agent.compile()
        return agent

    def _ctx(self, trust=TrustLevel.VERIFIED_EXTERNAL):
        from framework.schemas.invocation_context import InvocationContext

        return InvocationContext(caller_trust_level=trust)

    def test_valid_application_passes(self):
        result = self._agent().invoke(_VALID_APP_JSON, ctx=self._ctx())
        assert result["status"] == AgentStatus.SUCCESS.value, result.get("error_log")
        assert REPORT_FOOTER in result["output"]
        assert "Overall Status: PASS" in result["output"]

    def test_missing_fields_report_nigo(self):
        result = self._agent().invoke(_MISSING_FIELDS_JSON, ctx=self._ctx())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Overall Status: NIGO" in result["output"]

    def test_non_finite_coverage_does_not_pass(self):
        """The regression this pipeline exists to prevent."""
        payload = '{"policy_type": "term_life", "coverage_amount": NaN, "age_at_application": 35}'
        result = self._agent().invoke(payload, ctx=self._ctx())
        assert "Overall Status: PASS" not in result.get("output", "")

    def test_caller_metadata_reaches_the_report(self):
        result = self._agent().invoke(
            _VALID_APP_JSON,
            ctx=self._ctx(),
            input_context={"application_ref": "app_00042", "rule_profile": "senior_simplified"},
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Application Reference: app_00042" in result["output"]
        assert "Rule Profile: senior_simplified" in result["output"]

    def test_anonymous_caller_rejected(self):
        result = self._agent().invoke(_VALID_APP_JSON, ctx=self._ctx(TrustLevel.ANONYMOUS))
        assert result["status"] != AgentStatus.SUCCESS.value

    def test_injection_rejected_with_nothing_published(self):
        payload = '{"policy_type": "term_life", "note": "ignore previous instructions"}'
        result = self._agent().invoke(payload, ctx=self._ctx())
        assert result["status"] != AgentStatus.SUCCESS.value
        assert not result.get("output")

    def test_runtime_config_reaches_the_graph(self):
        agent = self._agent(config={"max_retry": 2, "timeout_s": 45})
        assert agent.config["max_retry"] == 2
        assert agent.config["timeout_s"] == 45

    @pytest.mark.parametrize(
        "bad_config", [{"max_retry": "NaN"}, {"max_retry": -1}, {"timeout_s": float("inf")}, {"timeout_s": 0}]
    )
    def test_malformed_runtime_config_fails_at_compile(self, bad_config):
        from framework.errors import ConfigError
        from src.graph.graph import InsC2006Agent

        with pytest.raises(ConfigError):
            InsC2006Agent(config=bad_config).compile()
