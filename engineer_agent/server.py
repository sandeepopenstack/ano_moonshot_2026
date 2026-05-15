# import os, sys, uvicorn
# from uuid import uuid4
# from google.adk.cli.fast_api import get_fast_api_app

# sys.path.insert(0, os.path.dirname(__file__))

# from ran_healing_shared.events import (
#     NETWORK_STATUS_KEY, EVENT_BUS_KEY, latest_key,
#     EVT_DETECTIVE_RCA_CONFIRMED,
# )
# from engineer_agent.tools import generate_healing_plan

# parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# app = get_fast_api_app(agents_dir=parent_dir, web=False, a2a=True)


# class _Ctx:
#     def __init__(self, state):
#         self.state = state


# @app.post("/api/engineer")
# def engineer_invoke(payload: dict):
#     rca_event = {
#         "event_id": str(uuid4()),
#         "event_type": EVT_DETECTIVE_RCA_CONFIRMED,
#         "source": "DetectiveAgent",
#         "payload": payload,
#         "network_status": "HEALING",
#     }
#     state = {
#         NETWORK_STATUS_KEY: "HEALING",
#         EVENT_BUS_KEY: [],
#         latest_key(EVT_DETECTIVE_RCA_CONFIRMED): rca_event,
#     }
#     ctx = _Ctx(state)
#     result = generate_healing_plan(ctx)
#     return {
#         "status": result.get("status"),
#         "engineer_output": state.get("engineer_output", {}),
#         "executor_payload": state.get("engineer_output", {}).get("tmf921_intent", {}),
#         "network_status": state.get(NETWORK_STATUS_KEY),
#     }


# if __name__ == "__main__":
#     uvicorn.run(app, host="0.0.0.0", port=8000)

"""
server.py — EngineerAgent Deployment Server
=============================================
FastAPI deployment service for EngineerAgent.

"""

import os
import sys
import uuid
import logging
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ── Import path fix ───────────────────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault("AGENT_MODEL", "gemini-2.5-flash")

# ── ADK imports ───────────────────────────────────────────────────────────────
from google.adk.agents.invocation_context import InvocationContext
from google.adk.sessions import InMemorySessionService

# ── Project imports — verified against agent.py and tools.py ─────────────────
from tools import generate_healing_plan
from engineer_agent.agent import root_agent as engineer_agent

from ran_healing_shared.events import (
    EVT_DETECTIVE_RCA_CONFIRMED,   # tools.py: consume_latest reads this
    EVT_ENGINEER_READY,            # tools.py: make_engineer_event writes this
    EVT_REFLEX_TRIAGE_READY,       # optional — carries ReflexAgent priority
    EVENT_BUS_KEY,                 # state key for event bus list
    NETWORK_STATUS_KEY,            # state key for pipeline status
    latest_key,                    # builds the state key for latest event
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="EngineerAgent Service — RAN Self-Healing",
    description=(
        "Step 7. Receives detective.rca.confirmed event, "
        "scores healing branches using utility formula "
        "(impact × criticality × (1-risk) × reversibility), "
        "builds TMF921 remediation intent, "
        "returns engineer.ready for ExecutorAgent."
    ),
    version="1.0.0",
)


# ── Pydantic input models ─────────────────────────────────────────────────────

class RcaEventRequest(BaseModel):
    """
    Request body for POST /engineer_event.
    """
    rca_event:    Dict[str, Any]           = Field(...,  description="detective.rca.confirmed event")
    reflex_event: Optional[Dict[str, Any]] = Field(None, description="reflex.triage.ready event (optional)")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Liveness probe for Cloud Run / Kubernetes."""
    return {"status": "ok", "agent": "EngineerAgent"}


@app.get("/")
async def root():
    return {
        "agent":    "EngineerAgent",
        "version":  "1.0.0",
        "endpoint": "POST /engineer_event",
        "input":    "Step 6 — detective.rca.confirmed event",
        "output":   "Step 7 — engineer.ready event → ExecutorAgent",
        "tool":     "generate_healing_plan (utility scoring + TMF921 intent)",
        "flow":     "Utility = impact × criticality × (1-risk) × reversibility → ranked branches",
    }


@app.post("/engineer_event")
async def engineer_event(request: RcaEventRequest):
    """
    Step 6 → Step 7 — EngineerAgent healing plan generation.

    Runs engineer_agent (LlmAgent) via InvocationContext + _run_async_impl.
    LLM calls generate_healing_plan exactly once (per agent.py instruction).

    Returns engineer.ready event for ExecutorAgent POST /execute-healing-plan.
    """
    rca_event    = request.rca_event
    reflex_event = request.reflex_event
    rca_payload  = rca_event.get("payload", {})

    logger.info(
        f"[/engineer_event] eventId={rca_payload.get('eventId')} "
        f"root_cause={rca_payload.get('root_cause')} "
        f"domain={rca_payload.get('domain')}"
    )

    # ── Seed session state ────────────────────────────────────────────────────
    state: Dict[str, Any] = {
        NETWORK_STATUS_KEY:                          "HEALING",
        EVENT_BUS_KEY:                               [rca_event],
        latest_key(EVT_DETECTIVE_RCA_CONFIRMED):     rca_event,
    }

    # ── Inject ReflexAgent context into state (optional) ─────────────────────
    # generate_healing_plan reads priority and GNN criticality from state
    # when available — same injection as engineer_service.py reference.
    if reflex_event:
        reflex_payload = reflex_event.get("payload", {})
        state["request_priority"]  = reflex_payload.get("priority", "CRITICAL")
        state["priority_external"] = reflex_payload.get("priority_external", "CRITICAL")
        state["priority_flag"]     = reflex_payload.get("priority_flag", "P1")
        state["reflex_output"]     = reflex_event
        # Carry latest_gnn_result so _get_gnn_criticality_label can read it
        if reflex_payload.get("latest_gnn_result"):
            state["latest_gnn_result"] = reflex_payload["latest_gnn_result"]
    else:
        state["request_priority"] = rca_payload.get("priority", "CRITICAL")

    # ── Create session ────────────────────────────────────────────────────────
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="engineer_agent_app",
        user_id="engineer_agent_user",
        state=state,
    )

    # ── InvocationContext — same pattern as reflexagent_service.py ────────────
    ctx = InvocationContext(
        session=session,
        session_service=session_service,
        invocation_id=str(uuid.uuid4()),
        agent=engineer_agent,
    )

    # ── Run EngineerAgent ─────────────────────────────────────────────────────
    # LlmAgent: LLM reads instruction → calls generate_healing_plan once → done.
    # generate_healing_plan writes state["engineer_output"] + publishes EVT_ENGINEER_READY.
    event = None
    async for evt in engineer_agent._run_async_impl(ctx):
        event = evt

    # ── Re-read session state after agent completes ───────────────────────────
    session = await session_service.get_session(
        app_name="engineer_agent_app",
        user_id="engineer_agent_user",
        session_id=session.id,
    )

    engineer_output    = session.state.get("engineer_output", {})
    engineer_event_out = session.state.get(latest_key(EVT_ENGINEER_READY), {})

    if not engineer_output:
        logger.error("[/engineer_event] No engineer_output in state after agent run")
        return JSONResponse(
            status_code=500,
            content={"error": "EngineerAgent produced no output. Check that detective.rca.confirmed event is correctly structured."},
        )

    return JSONResponse(content={
        "status":  "processed",
        "agent":   "EngineerAgent",
        # Full published engineer.ready event — pass this to ExecutorAgent
        "output_event": engineer_event_out,
        # Raw engineer output — all fields for ExecutorAgent consumption
        "engineer_output": {
            "eventId":               engineer_output.get("eventId"),
            "intent_type":           engineer_output.get("intent_type"),
            "root_cause":            engineer_output.get("root_cause"),
            "root_cause_mapped":     engineer_output.get("root_cause_mapped"),
            "domain":                engineer_output.get("domain"),
            "priority":              engineer_output.get("priority"),
            "target_entities":       engineer_output.get("target_entities", []),
            "tmf921_intent":         engineer_output.get("tmf921_intent", {}),
            "ranked_healing_branches": engineer_output.get("ranked_healing_branches", []),
            "execution_order":       engineer_output.get("execution_order", []),
            "utility_scoring":       engineer_output.get("utility_scoring", {}),
            "recovery_targets":      engineer_output.get("recovery_targets", []),
            "change_request_id":     engineer_output.get("change_request_id"),
            "hypothesis_id":         engineer_output.get("hypothesis_id"),
            "confidence_score":      engineer_output.get("confidence_score"),
            "evidence_chain":        engineer_output.get("evidence_chain", []),
        },
        # Convenience fields
        "intent_id":        engineer_output.get("tmf921_intent", {}).get("intent_id"),
        "branch_count":     engineer_output.get("utility_scoring", {}).get("branch_count"),
        "top_utility_score": engineer_output.get("utility_scoring", {}).get("top_utility_score"),
    })


# ── Dev entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        log_level="info",
    )