"""PB-6: Backbone invoke-order verification for INS-C2-006.

Asserts that Graph().invoke() with a VERIFIED_EXTERNAL caller exercises the
correct node execution order:
  InitializeNode → PreProcessNode → ParseNode
  → UnderwritingRuleValidateNode → RiskFlagNode → GenerateReportNode
  → PostProcessNode → FinalizeNode

Uses a real Graph().invoke() over a SUCCESS-yielding payload with
InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL).

⚠️ DO NOT use InvocationContext.for_internal() — an INTERNAL context masks
   the inner-node trust-trap and makes the test a false green.
"""

import json

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Template-specific constants — the two things to update per build
# ─────────────────────────────────────────────────────────────────────────────

# The class that fills the AgentBaseGraph `main` slot (FunctionNode, not GraphNode)
from src.nodes.parse_node import ParseNode as _MAIN_SLOT_NODE_CLS

_MAIN_SLOT_NODE_NAME = _MAIN_SLOT_NODE_CLS.__name__  # "ParseNode"

# A valid payload that produces AgentStatus.SUCCESS through the full pipeline
_VALID_PAYLOAD = json.dumps(
    {
        "policy_type": "term_life",
        "coverage_amount": 10000000,
        "age_at_application": 35,
    }
)

# Expected backbone order (all flat domain nodes included)
_EXPECTED_NODE_ORDER = [
    "InitializeNode",
    "PreProcessNode",
    "ParseNode",
    "UnderwritingRuleValidateNode",
    "RiskFlagNode",
    "GenerateReportNode",
    "PostProcessNode",
    "FinalizeNode",
]


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _noop_emit(monkeypatch):
    """Silence emit_trace_event in all node modules so tests run without backends."""
    import framework.nodes.base_node as base_node_module
    import src.nodes.pre_process_node as m1
    import src.nodes.parse_node as m2
    import src.nodes.underwriting_rule_validate_node as m3
    import src.nodes.risk_flag_node as m4
    import src.nodes.generate_report_node as m5
    import src.nodes.post_process_node as m6

    def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(base_node_module, "emit_trace_event", noop)
    for mod in (m1, m2, m3, m4, m5, m6):
        monkeypatch.setattr(mod, "emit_trace_event", noop)


def _build_agent():
    """Instantiate and compile the graph."""
    from src.graph.graph import InsC2006Agent

    agent = InsC2006Agent()
    agent.compile()
    return agent


def _external_ctx():
    """Real VERIFIED_EXTERNAL InvocationContext — same as a production caller."""
    from framework.schemas.invocation_context import InvocationContext
    from framework.schemas.trust_level import TrustLevel

    return InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)


# ─────────────────────────────────────────────────────────────────────────────
# PB-6 tests
# ─────────────────────────────────────────────────────────────────────────────


class TestPB6InvokeOrder:
    """Verify the full backbone node-execution order via Graph().invoke()."""

    def test_node_history_order_on_success(self):
        """Valid payload → full pipeline runs in the expected order."""
        from framework.schemas.agent_status import AgentStatus

        result = _build_agent().invoke(_VALID_PAYLOAD, ctx=_external_ctx())

        assert result["status"] == AgentStatus.SUCCESS.value, (
            f"Expected SUCCESS; got {result['status']}. " f"output={result.get('output')!r}"
        )
        assert result.get("output") is not None, "output must be non-None on SUCCESS"
        assert "[UNDERWRITING VALIDATION COMPLETED]" in result.get("output", "")

        node_history = result.get("node_history", [])
        assert node_history == _EXPECTED_NODE_ORDER, (
            f"Backbone node order mismatch.\n" f"  Expected: {_EXPECTED_NODE_ORDER}\n" f"  Actual:   {node_history}"
        )

    def test_anonymous_caller_denied(self):
        """ANONYMOUS caller must be denied by the PreProcessNode trust gate."""
        from framework.schemas.agent_status import AgentStatus
        from framework.schemas.invocation_context import InvocationContext
        from framework.schemas.trust_level import TrustLevel

        ctx = InvocationContext(caller_trust_level=TrustLevel.ANONYMOUS)
        result = _build_agent().invoke(_VALID_PAYLOAD, ctx=ctx)

        assert (
            result["status"] != AgentStatus.SUCCESS.value
        ), "ANONYMOUS caller must NOT reach SUCCESS — the VERIFIED_EXTERNAL trust gate must deny it"

    def test_main_slot_node_is_function_node(self):
        """Main slot node is a plain FunctionNode, not a GraphNode (flat Cat-2)."""
        from framework.nodes.function_node import FunctionNode

        assert issubclass(
            _MAIN_SLOT_NODE_CLS, FunctionNode
        ), f"{_MAIN_SLOT_NODE_NAME} must be a FunctionNode subclass (flat Cat-2 — no nested subgraph)"

    def test_output_key_is_output(self):
        """result['output'] is populated (not 'formatted_output') via AgentBaseGraph.get_output()."""
        from framework.schemas.agent_status import AgentStatus

        result = _build_agent().invoke(_VALID_PAYLOAD, ctx=_external_ctx())

        assert result["status"] == AgentStatus.SUCCESS.value
        # The invoke surface exposes 'output' (mapped from state.formatted_output)
        assert "output" in result, "result must have 'output' key"
        assert result["output"] is not None

    def test_empty_input_is_declined_without_any_domain_work(self):
        """Empty input is declined at the input boundary.

        The run COMPLETES carrying the reason, so the caller reads what to send
        and can submit an application on the same conversation. Every node after
        the boundary passes straight through: the pipeline is entered but no
        domain work is done and nothing is published.
        """
        from framework.schemas.agent_status import AgentStatus

        result = _build_agent().invoke("", ctx=_external_ctx())

        assert result["status"] == AgentStatus.SUCCESS.value
        assert result.get("output"), result
        # Nothing derived from an application: no parse, no rule outcome, no
        # risk flags, no report. Those keys are absent, not present and empty.
        for produced in ("input_record", "validation_results", "risk_flags", "report_output"):
            assert not result.get(produced), produced
