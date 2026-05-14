"""
reflexagent_service.py
========================
FastAPI deployment service for ReflexAgent.

Pattern:
  1. Pydantic model validates Step 2b input
  2. Seed session state with failure_notification event
     (mirrors: publish_event(state, make_gnn_anomaly_event(gnn_dict)))
  3. Create InMemorySessionService + session
  4. Create InvocationContext with reflex_agent
  5. Run reflex_agent._run_async_impl(ctx)
     → call_gnn_engine → perform_triage → publish_triage (3 tools in order)
  6. Read state["triage_result"] after agent completes
  7. Return detective_payload -> POST /investigate (Ericsson Detective Agent)

ReflexAgent flow (Steps 2b → 5):
  call_gnn_engine  → GNN inference (Step 4a / 4b)
                     reads:  state[latest_key(EVT_FAILURE_NOTIFICATION)]
                     writes: state["latest_gnn_result"], state["pre_action_z_score"]
  perform_triage   → MCP/Spanner domain triage (Step 5)
                     reads:  state["latest_gnn_result"]
                     writes: state["triage_result"]
  publish_triage   → publishes reflex.triage.ready event
                     reads:  state["triage_result"]
                     writes: state["reflex_output"], state[NETWORK_STATUS_KEY]="HEALING"

Output -> Detective Agent POST /investigate:
  eventId, entity_ids, anomalous_subgraph, ranked_list,
  domain_triage, priority_flag, priority,
  impact_score, criticality_score, criticality_label, reference_time
"""

import os
import sys
import uuid
import logging
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ── Import path fix (same as monitoring_agent/server.py) ─────────────────────
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault("AGENT_MODEL", "gemini-2.5-flash")

# ── ADK imports ───────────────────────────────────────────────────────────────
from google.adk.agents.invocation_context import InvocationContext
from google.adk.sessions import InMemorySessionService

# ── Project imports — verified against tools.py and agent.py ─────────────────
from app.reflex_agent.agent import reflex_agent   # LlmAgent, name="ReflexAgent"

from app.events import (
    EVT_FAILURE_NOTIFICATION,         # tools.py line 40: call_gnn_engine reads this
    EVT_REFLEX_TRIAGE_READY,          # tools.py line 41: publish_triage writes this
    NETWORK_STATUS_KEY,               # main.py + tools.py
    EVENT_BUS_KEY,                    # main.py
    publish_event,                    # tools.py line 44
    latest_key,                       # tools.py line 45
    make_failure_notification_event,  # main.py
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="ReflexAgent Service — RAN Self-Healing",
    description=(
        "Step 2b → Step 5. "
        "Receives FailureInjectionCreateEvent, runs GNN inference (Step 4a/4b), "
        "queries Spanner via MCP for domain triage (Step 5), "
        "returns triage payload for Detective Agent (Ericsson A2A)."
    ),
    version="1.0.0",
)


# ── Pydantic input models ─────────────────────────────────────────────────────
# Mirrors monitoring_agent pattern:
#   monitoring: class GNNEventModel(BaseModel) → class GNNRequest(BaseModel)
#   reflex:     class FailureInjectionEvent(BaseModel) → class ReflexRequest(BaseModel)

class FailureInjectionEvent(BaseModel):
    """
    Step 2b — FailureInjectionCreateEvent.
    Same fields as TRIGGER_EVENTS dict in main.py.
    Sent by Failure Injection MS to trigger the healing pipeline.
    """
    id:                        Optional[str]       = Field(None)
    eventId:                   str                 = Field(...,  description="Unique event ID e.g. EV-dekfn_efjnf_fefe")
    eventTime:                 Optional[str]       = Field(None, description="ISO8601 timestamp")
    eventType:                 Optional[str]       = Field("FailureInjectionCreateEvent")
    sourceSystem:              Optional[str]       = Field("FAILURE_INJECTION_MS")
    probableDomain:            Optional[str]       = Field(None, description="RAN / CORE / TRANSPORT / CROSS_DOMAIN")
    trigger:                   str                 = Field(...,  description="antenna_tilt_misconfiguration / hss_failover / fiber_cut")
    useCaseId:                 Optional[str]       = Field(None, description="uc1 / uc2 / uc3")
    domain:                    Optional[str]       = Field(None)
    affected_layers:           Optional[List[str]] = Field(default_factory=list)
    affected_core_elements:    Optional[List[str]] = Field(default_factory=list)
    affected_enodebs:          Optional[List[str]] = Field(default_factory=list)
    affected_neighbor_enodebs: Optional[List[str]] = Field(default_factory=list)


class ReflexRequest(BaseModel):
    """
    Request wrapper.
    Mirrors monitoring_agent: { "gnn": GNNEventModel }
    Reflex:                   { "event": FailureInjectionEvent }
    """
    event: FailureInjectionEvent = Field(
        ...,
        description="Step 2b FailureInjectionCreateEvent payload from Failure Injection MS",
    )


# ── Health + info endpoints ───────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Liveness probe — Cloud Run / Kubernetes."""
    return {"status": "ok", "agent": "ReflexAgent"}


@app.get("/")
async def root():
    return {
        "agent":    "ReflexAgent",
        "version":  "1.0.0",
        "endpoint": "POST /trigger_event",
        "input":    "Step 2b — FailureInjectionCreateEvent",
        "output":   "Step 5 — Triage payload for Detective Agent (Ericsson A2A)",
        "flow":     "call_gnn_engine → perform_triage → publish_triage",
    }


# ── Core endpoint ─────────────────────────────────────────────────────────────

@app.post("/trigger_event")
async def trigger_event(request: Dict[str, Any]):
    """
    Step 2b → Step 5 — ReflexAgent triage.

    Runs ReflexAgent with 3 tools in strict order:
      call_gnn_engine  → GNN inference (Step 4a/4b)
      perform_triage   → MCP/Spanner domain triage (Step 5)
      publish_triage   → publishes reflex.triage.ready

    Returns detective_payload for Detective Agent POST /investigate.

    Example request:
    {
      "event": {
        "eventId": "EV-dekfn_efjnf_fefe",
        "trigger": "antenna_tilt_misconfiguration",
        "affected_enodebs": ["eNB-SYN-003", "eNB-SYN-004", "eNB-SYN-005"],
        "affected_core_elements": [],
        "affected_neighbor_enodebs": ["eNB-SYN-002", "eNB-SYN-008"]
      }
    }
    """
    raw_event = request.get("event", request)
    event = FailureInjectionEvent.model_validate(raw_event)
    event_dict = event.model_dump()

    logger.info(
        f"[/trigger_event] eventId={event.eventId} "
        f"trigger={event.trigger} "
        f"enodebs={event.affected_enodebs} "
        f"core={event.affected_core_elements}"
    )

    # ── Step 1: Seed session state ────────────────────────────────────────────

    state: Dict[str, Any] = {}
    failure_event = make_failure_notification_event(event_dict)
    publish_event(state, failure_event)

    # Set latest_key so call_gnn_engine can find the event immediately
    state[latest_key(EVT_FAILURE_NOTIFICATION)] = failure_event
    state[NETWORK_STATUS_KEY]                   = "ANOMALY_DETECTED"
    state[EVENT_BUS_KEY]                        = [failure_event]

    # ── Step 2: Create session ────────────────────────────────────────────────
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="reflex_agent_app",
        user_id="reflex_agent_user",
        state=state,
    )

    # ── Step 3: InvocationContext — same as monitoring_agent ──────────────────
    ctx = InvocationContext(
        session=session,
        session_service=session_service,
        invocation_id=str(uuid.uuid4()),
        agent=reflex_agent,
    )

    # ── Step 4: Run ReflexAgent ───────────────────────────────────────────────
    # ADK runs 3 tools in strict order defined in agent.py instruction:
    #   call_gnn_engine → reads failure_event → calls GNN → writes latest_gnn_result
    #   perform_triage  → reads latest_gnn_result → queries Spanner → writes triage_result
    #   publish_triage  → reads triage_result → publishes EVT_REFLEX_TRIAGE_READY
    event = None
    async for evt in reflex_agent._run_async_impl(ctx):
        event = evt

    # ── Step 5: Re-read session state after agent completes ───────────────────
    session = await session_service.get_session(
        app_name="reflex_agent_app",
        user_id="reflex_agent_user",
        session_id=session.id,
    )
    triage_result = session.state.get("triage_result", {})

    if not triage_result:
        logger.error("[/trigger_event] ReflexAgent produced no triage_result in state")
        return JSONResponse(
            status_code=500,
            content={"error": "ReflexAgent produced no triage result."},
        )

    # ── Step 6: Build response ────────────────────────────────────────────────
    result = {
        # Core identification
        "eventId":               triage_result.get("eventId"),
        # entity_ids = plain list of EID strings for Detective Agent.
        "entity_ids":            triage_result.get("entity_ids", []),
        # Full anomalous subgraph (nodes + edges) from GNN Step 4b
        "anomalous_subgraph": {
            "nodes": triage_result.get("raw_nodes", []),
            "edges": triage_result.get("raw_edges", []),
        },
        # GNN ranked list with priority flags (P1=CRITICAL, P2=HIGH, P3=MEDIUM)
        "ranked_list":           triage_result.get("ranked_list", []),
        # Domain triage result (Step 5 output)
        "domain_triage":         triage_result.get("domain_triage"),
        "priority_flag":         triage_result.get("priority_flag"),
        "priority":              triage_result.get("priority"),
        # KPI impact scores from INSIGHT.csv via GNN
        "impact_score":          triage_result.get("impact_score"),
        "criticality_score":     triage_result.get("criticality_score"),
        "criticality_label":     triage_result.get("criticality_label"),
        "reference_time":        triage_result.get("reference_time"),
        # Context
        "affected_domains":      triage_result.get("affected_domains", []),
        "spanner_source":        triage_result.get("spanner_source"),
        "impact_radius":         triage_result.get("spanner_impact_radius"),
    }

    return JSONResponse(content=result)


# ── Dev entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(
        "app.reflex_agent.reflex_service:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        log_level="info",
    )
