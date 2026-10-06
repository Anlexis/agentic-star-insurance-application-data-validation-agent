"""AgentCore Platform v1.0 — INS-C2-006 standalone HTTP entry point."""

# Entry point adapter only — no business logic here.
# For platform-level routing, AgentGateway calls agent.invoke() directly.
#
# The adapter does three things beyond forwarding the request: it loads the
# runtime parameters so the standalone server matches the platform deployment,
# it caps the size of the caller-metadata channel, and it promotes an
# authenticated caller to the trust level the input boundary requires.
#
# Class name must match config/agent.yaml `class: src.graph.graph.InsC2006Agent`.

import json
import os
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory

from src.graph.graph import InsC2006Agent

# Serialized size cap for the caller-metadata channel — enforced at the adapter
# so an oversized payload never reaches the graph.
_MAX_INPUT_CONTEXT_BYTES = 256 * 1024


def _load_runtime_config() -> dict[str, Any]:
    """Load config/config.yaml — the runtime parameters (max_retry, timeout_s).

    The platform registry loads this file itself and passes it to the graph
    constructor; the standalone server must do the same, or the declared values
    silently never reach the graph.
    """
    config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
    if not config_path.exists():
        return {}
    with open(config_path, encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    return loaded if isinstance(loaded, dict) else {}


app = FastAPI(title="INS-C2-006 — Insurance Application Data Validation Agent")

agent = InsC2006Agent(config=_load_runtime_config())
agent.compile()
agent.provision_secrets(secrets_factory(namespace="ins", agent_name="InsuranceApplicationDataValidationAgent"))


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Structured caller metadata (channel, submitter_ref, application_ref,
    # rule_profile) — validated field by field in PreProcessNode; the adapter
    # only enforces the size cap.
    input_context: dict[str, Any] = Field(default_factory=dict)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> dict[str, Any]:
    context_size = len(json.dumps(req.input_context, ensure_ascii=False).encode("utf-8"))
    if context_size > _MAX_INPUT_CONTEXT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"input_context exceeds the {_MAX_INPUT_CONTEXT_BYTES}-byte limit",
        )

    with bound_secrets(agent._secrets_provider):
        # Trust promotion: callers that present a valid INVOKE_AUTH_TOKEN Bearer
        # credential are VERIFIED_EXTERNAL — the minimum trust PreProcessNode
        # requires. Unauthenticated requests remain ANONYMOUS. When a
        # higher-trust middleware (e.g. AgentGateway) already set
        # request.state.trust_level it takes precedence over that default.
        env_token = os.environ.get("INVOKE_AUTH_TOKEN", "")
        auth_header = request.headers.get("Authorization", "")
        if env_token and auth_header == f"Bearer {env_token}":
            trust = TrustLevel.VERIFIED_EXTERNAL
        else:
            trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)

        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        # invoke() comes from the untyped framework package; its result is the
        # documented output mapping.
        return cast(
            dict[str, Any],
            agent.invoke(req.input, ctx=ctx, input_context=req.input_context),
        )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "InsC2006Agent"}
