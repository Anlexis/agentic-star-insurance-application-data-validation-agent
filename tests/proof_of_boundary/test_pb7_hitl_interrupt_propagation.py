"""PB-7: HITL cross-boundary interrupt propagation — INS-C2-006.

INS-C2-006 is a synchronous flat Cat-2 pipeline with no cross-boundary
Human-In-The-Loop (HITL) interruption mechanism.  This file documents that
design decision as a skip stub, so the absence of HITL assertions is a
recorded architectural decision rather than a gap in the suite.

If a future version of this template adds HITL support
(i.e., ``InsC2006Agent.propagate_hitl = True``), remove the ``skipif``
condition and implement the real interrupt-propagation assertions here.
"""

import pytest


def _hitl_propagation_enabled() -> bool:
    """Return True only if the agent has opted into HITL propagation."""
    try:
        from src.graph.graph import InsC2006Agent

        return getattr(InsC2006Agent, "propagate_hitl", False)
    except ImportError:
        return False


@pytest.mark.skipif(
    not _hitl_propagation_enabled(),
    reason=(
        "INS-C2-006 does not implement cross-boundary HITL interrupt propagation "
        "(synchronous underwriting validation pipeline — no HITL step required). "
        "The skip is intentional."
    ),
)
class TestPB7HitlInterruptPropagation:
    """PB-7: Verify that a human-interrupt signal propagates correctly across node boundaries.

    This test class body is intentionally empty — the skipif condition above
    ensures it is skipped for this template.  A future HITL-enabled version
    should implement real propagation assertions inside this class.
    """

    def test_hitl_interrupt_propagates_across_boundary(self):
        """Placeholder: implement when InsC2006Agent.propagate_hitl = True."""
        pytest.skip("HITL propagation not applicable for INS-C2-006 (no HITL step).")
