"""AgentCore Platform v1.0 — INS-C2-006 outer graph (flat Cat 2)."""

# Flat Cat-2 pipeline — AgentBaseGraph with a six-node linear domain chain.
#
# Architecture:
#     START → initialize → pre_process → [parse] → uw_validate → risk_flag
#                                                 → generate_report → post_process
#                                                 → finalize → END
#
#   This is a FLAT Cat-2: all six domain nodes are plain FunctionNode
#   subclasses registered directly in register_nodes(). There is no GraphNode
#   wrapping an inner graph, so the caller's input_context reaches every node
#   through the shared state without any bridging.
#
# Slot mapping:
#   pre_process     ← PreProcessNode                 (input boundary; VERIFIED_EXTERNAL)
#   main            ← ParseNode                      (backbone `main` slot)
#   uw_validate     ← UnderwritingRuleValidateNode   (apply the rules; ANONYMOUS)
#   risk_flag       ← RiskFlagNode                   (derive flags; ANONYMOUS)
#   generate_report ← GenerateReportNode             (compile the report; ANONYMOUS)
#   post_process    ← PostProcessNode                (output boundary; ANONYMOUS)
#
# Class-name contract:
#   graph.py class          : InsC2006Agent
#   config/agent.yaml class : src.graph.graph.InsC2006Agent
#   src/api/server.py import: from src.graph.graph import InsC2006Agent
#
# add_edges() override:
#   Conditional edge at pre_process: SUCCESS→main, else→finalize.
#   Linear chain from main (parse) → uw_validate → risk_flag → generate_report.
#   Standard post_process → finalize → END.
#   Do NOT call add_edges() from super() — this graph fully owns the wiring.
#
# Rules:
#   - Outer graph inherits AgentBaseGraph
#   - Call super().register_nodes() (injects InitializeNode + FinalizeNode)
#   - All three backbone slots filled: pre_process / main / post_process
#   - Extra domain nodes registered by name (uw_validate, risk_flag, generate_report)
#   - Never import the platform SDK — framework.*, shared.* and src.* only

from framework.errors import ConfigError
from framework.graph.agent_base_graph import AgentBaseGraph
from framework.schemas.agent_status import AgentStatus

from src.nodes.generate_report_node import GenerateReportNode
from src.nodes.parse_node import ParseNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.risk_flag_node import RiskFlagNode
from src.nodes.underwriting_rule_validate_node import UnderwritingRuleValidateNode
from src.schemas.state import State, finite_in_range

# Outer bounds for the runtime parameters declared in config/config.yaml.
_MAX_RETRY_MAX = 9
_TIMEOUT_S_MAX = 3600


class InsC2006Agent(AgentBaseGraph):
    """INS-C2-006 Insurance Application Data Validation Agent.

    Flat Cat-2 pipeline:
      PreProcessNode → ParseNode → UnderwritingRuleValidateNode
        → RiskFlagNode → GenerateReportNode → PostProcessNode

    Class name matches:
      config/agent.yaml  `class: src.graph.graph.InsC2006Agent`
      src/api/server.py  `from src.graph.graph import InsC2006Agent`
    """

    @property
    def name(self) -> str:
        return "INS-C2-006"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        """Validate the runtime parameters from config/config.yaml.

        The pipeline itself needs no mandatory configuration, but a declared
        value that is malformed must fail at compile time rather than reach the
        backbone. Absent keys are legitimate — the framework defaults apply.
        """
        max_retry = self.config.get("max_retry")
        if max_retry is not None:
            parsed_retry = finite_in_range(max_retry, 0, _MAX_RETRY_MAX)
            if parsed_retry is None or parsed_retry != int(parsed_retry):
                raise ConfigError(
                    f"[{self.__class__.__name__}] 'max_retry' must be a whole number " f"between 0 and {_MAX_RETRY_MAX}"
                )
        timeout_s = self.config.get("timeout_s")
        if timeout_s is not None and finite_in_range(timeout_s, 1, _TIMEOUT_S_MAX) is None:
            raise ConfigError(
                f"[{self.__class__.__name__}] 'timeout_s' must be a finite number " f"between 1 and {_TIMEOUT_S_MAX}"
            )
        super()._validate_config()

    def register_nodes(self) -> None:
        """Register the six-node flat domain pipeline.

        super().register_nodes() injects InitializeNode (initialize slot)
        and FinalizeNode (finalize slot) — both are required.
        The backbone `main` slot is filled with ParseNode; the additional
        domain nodes (uw_validate, risk_flag, generate_report) are registered
        as named extra slots and wired in add_edges().
        """
        super().register_nodes()  # injects InitializeNode + FinalizeNode (required)

        # Backbone required slots
        self._nodes["pre_process"] = PreProcessNode()  # input boundary
        self._nodes["main"] = ParseNode()  # parse step (main slot)
        self._nodes["post_process"] = PostProcessNode()  # output boundary

        # Extra domain nodes (flat chain — no inner graph)
        self._nodes["uw_validate"] = UnderwritingRuleValidateNode()
        self._nodes["risk_flag"] = RiskFlagNode()
        self._nodes["generate_report"] = GenerateReportNode()

    def add_edges(self) -> None:
        """Wire the flat six-node domain pipeline.

        Conditional edge after pre_process:
          SUCCESS   → main (ParseNode)
          otherwise → finalize (short-circuit — the input boundary refused)

        Linear chain for the domain nodes (no retry, no human-in-the-loop step
        in this agent):
          main → uw_validate → risk_flag → generate_report → post_process

        Standard tail:
          post_process → finalize → END
        """
        from langgraph.graph import END, START

        self._sg.add_edge(START, "initialize")
        self._sg.add_edge("initialize", "pre_process")

        # Conditional gate: a refused input skips the whole domain pipeline.
        self._sg.add_conditional_edges(
            "pre_process",
            lambda s: "main" if s.get("status") == AgentStatus.SUCCESS.value else "finalize",
        )

        # Flat domain chain (linear — no branching within the domain steps)
        self._sg.add_edge("main", "uw_validate")  # parse → uw_validate
        self._sg.add_edge("uw_validate", "risk_flag")
        self._sg.add_edge("risk_flag", "generate_report")
        self._sg.add_edge("generate_report", "post_process")

        # Backbone tail
        self._sg.add_edge("post_process", "finalize")
        self._sg.add_edge("finalize", END)


# Alias kept for entry points that import a generic name.
Graph = InsC2006Agent

__all__: list[str] = ["Graph", "InsC2006Agent"]
