# import os, sys, uvicorn
# from uuid import uuid4
# from google.adk.cli.fast_api import get_fast_api_app

# sys.path.insert(0, os.path.dirname(__file__))

# from ran_healing_shared.events import (
#     NETWORK_STATUS_KEY, EVENT_BUS_KEY, latest_key,
#     make_failure_notification_event,
# )
# from ran_healing_shared.failure_injection_ms import build_trigger_event
# from reflex_agent.tools import call_gnn_engine, perform_triage, publish_triage

# parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# app = get_fast_api_app(agents_dir=parent_dir, web=False, a2a=True)


# class _Ctx:
#     def __init__(self, state):
#         self.state = state


# @app.post("/api/reflex")
# def reflex_invoke(payload: dict = {}):
#     use_case = payload.get("use_case_id", "uc1")
#     trigger = build_trigger_event(use_case_id=use_case)
#     failure = make_failure_notification_event(trigger)
#     state = {
#         NETWORK_STATUS_KEY: "ANOMALY_DETECTED",
#         EVENT_BUS_KEY: [failure],
#         latest_key(failure["event_type"]): failure,
#     }
#     ctx = _Ctx(state)
#     call_gnn_engine(ctx)
#     perform_triage(ctx)
#     publish_triage(ctx)
#     reflex_event = state.get("reflex_output", {})
#     return {
#         "status": "ok",
#         "network_status": state.get(NETWORK_STATUS_KEY),
#         "detective_investigation_request": reflex_event.get("payload", {}),
#     }


# if __name__ == "__main__":
#     uvicorn.run(app, host="0.0.0.0", port=8000)

"""
server.py — ReflexAgent Deployment Server
==========================================
Deploys ReflexAgent as a standalone HTTP microservice on GCP.

ReflexAgent flow (Steps 2b → 5):
  call_gnn_engine  → GNN inference (Step 4a/4b)
  perform_triage   → MCP/Spanner domain triage (Step 5)
  publish_triage   → builds detective_payload, writes state["triage_result"]

Output → POST /investigation-request (Detective Agent):
  entity_ids, anomalous_subgraph, ranked_list,
  domain_triage, priority_flag, priority,
  impact_score, criticality_score, reference_time
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

from google.adk.agents.invocation_context import InvocationContext
from google.adk.sessions import InMemorySessionService

from reflex_agent.tools import call_gnn_engine, perform_triage, publish_triage
from reflex_agent.agent import root_agent as reflex_agent
from ran_healing_shared.events import (
    make_failure_notification_event,
    publish_event,
    NETWORK_STATUS_KEY,
    EVENT_BUS_KEY,
    EVT_FAILURE_NOTIFICATION,
    latest_key,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="ReflexAgent — RAN Self-Healing",
    description=(
        "Step 2b → Step 5. Receives FailureInjectionCreateEvent, "
        "runs GNN inference + Spanner triage via MCP, "
        "returns triage payload for Detective Agent."
    ),
    version="1.0.0",
)


# ── Pydantic models ───────────────────────────────────────────────────────────

class FailureInjectionEvent(BaseModel):
    """
    Step 2b — FailureInjectionCreateEvent.
    Same fields as TRIGGER_EVENTS dict in main.py.
    """
    id:                        Optional[str]       = Field(None)
    eventId:                   str                 = Field(...,  description="Unique event ID e.g. EV-001")
    eventTime:                 Optional[str]       = Field(None)
    eventType:                 Optional[str]       = Field("FailureInjectionCreateEvent")
    sourceSystem:              Optional[str]       = Field("FAILURE_INJECTION_MS")
    probableDomain:            Optional[str]       = Field(None)
    trigger:                   str                 = Field(...,  description="antenna_tilt_misconfiguration / hss_failover / fiber_cut")
    useCaseId:                 Optional[str]       = Field(None)
    domain:                    Optional[str]       = Field(None)
    affected_layers:           Optional[List[str]] = Field(default_factory=list)
    affected_core_elements:    Optional[List[str]] = Field(default_factory=list)
    affected_enodebs:          Optional[List[str]] = Field(default_factory=list)
    affected_neighbor_enodebs: Optional[List[str]] = Field(default_factory=list)


class ReflexRequest(BaseModel):
    """
    Request wrapper — mirrors monitoring_agent pattern:
      monitoring: { "gnn": GNNEventModel }
      reflex:     { "event": FailureInjectionEvent }
    """
    event: FailureInjectionEvent = Field(
        ..., description="Step 2b FailureInjectionCreateEvent payload"
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Liveness probe for Cloud Run / Kubernetes."""
    return {"status": "ok", "agent": "ReflexAgent"}


@app.get("/")
async def root():
    return {
        "agent":    "ReflexAgent",
        "version":  "1.0.0",
        "endpoint": "POST /trigger_event",
        "input":    "Step 2b — FailureInjectionCreateEvent",
        "output":   "Step 5 — Triage payload → Detective Agent",
    }


@app.post("/trigger_event")
async def triage(request: ReflexRequest):
    """
    Step 2b → Step 5 — ReflexAgent triage.

    Runs: call_gnn_engine → perform_triage → publish_triage
    Returns detective_payload (entity_ids, domain_triage, priority, etc.)

    Example request:
    {
      "event": {
        "eventId": "EV-001",
        "trigger": "antenna_tilt_misconfiguration",
        "affected_enodebs": ["eNB-SYN-003", "eNB-SYN-004"],
        "affected_core_elements": [],
        "affected_neighbor_enodebs": ["eNB-SYN-002"]
      }
    }
    """
    event_dict = request.event.model_dump()

    logger.info(
        f"[/triage] eventId={request.event.eventId} "
        f"trigger={request.event.trigger} "
        f"enodebs={request.event.affected_enodebs}"
    )

    # ── Seed session state — mirrors monitoring_agent exactly ─────────────────
    # monitoring: publish_event(state, make_gnn_anomaly_event(gnn_dict))
    # reflex:     publish_event(state, make_failure_notification_event(event_dict))
    state: Dict[str, Any] = {}
    failure_event = make_failure_notification_event(event_dict)
    publish_event(state, failure_event)

    state[latest_key(EVT_FAILURE_NOTIFICATION)] = failure_event
    state[NETWORK_STATUS_KEY]                   = "ANOMALY_DETECTED"
    state[EVENT_BUS_KEY]                        = [failure_event]

    # ── Create session ────────────────────────────────────────────────────────
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="reflex_agent_app",
        user_id="reflex_agent_user",
        state=state,
    )

    # ── InvocationContext — same as monitoring_agent ──────────────────────────
    ctx = InvocationContext(
        session=session,
        session_service=session_service,
        invocation_id=str(uuid.uuid4()),
        agent=reflex_agent,
    )

    # ── Run ReflexAgent ───────────────────────────────────────────────────────
    # Runs 3 tools: call_gnn_engine → perform_triage → publish_triage
    # publish_triage writes state["triage_result"] with the full detective payload
    event = None
    async for evt in reflex_agent._run_async_impl(ctx):
        event = evt

    # ── Re-read session state after agent completes ───────────────────────────
    session = await session_service.get_session(
        app_name="reflex_agent_app",
        user_id="reflex_agent_user",
        session_id=session.id,
    )
    triage_result = session.state.get("triage_result", {})

    if not triage_result:
        return JSONResponse(
            status_code=500,
            content={"error": "ReflexAgent produced no triage result."},
        )

    # ── Return the 9 fields that go to Detective Agent ────────────────────────
    # Mirrors monitoring_agent which returns:
    #   entity_ids, domain_triage, priority, reference_time
    # Reflex returns the full Step 5 → Detective Agent payload:
    result = {
        "eventId":            triage_result.get("eventId"),
        "entity_ids":         triage_result.get("raw_nodes", []),
        "anomalous_subgraph": {
            "nodes": triage_result.get("raw_nodes", []),
            "edges": triage_result.get("raw_edges", []),
        },
        "ranked_list":           triage_result.get("ranked_list", []),
        "domain_triage":         triage_result.get("domain_triage"),
        "priority_flag":         triage_result.get("priority_flag"),
        "priority":              triage_result.get("priority"),
        "impact_score":          triage_result.get("impact_score"),
        "criticality_score":     triage_result.get("criticality_score"),
        "criticality_label":     triage_result.get("criticality_label"),
        "reference_time":        triage_result.get("reference_time"),
        "affected_domains":      triage_result.get("affected_domains", []),
        "spanner_source":        triage_result.get("spanner_source"),
        "impact_radius":         triage_result.get("spanner_impact_radius"),
    }

    return JSONResponse(content=result)


# ── Dev entrypoint ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=True)
