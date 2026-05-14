"""
server.py — EngineerAgent Deployment Server
=============================================
FastAPI deployment service for EngineerAgent.

EngineerAgent flow (Step 7):
  generate_healing_plan tool (single tool, NO arguments):
    reads:   consume_latest(state, EVT_DETECTIVE_RCA_CONFIRMED)
    parses:  root_cause, affected_entities, risk_score, reversibility_score,
             suggested_remediation, recovery_targets, causal_parameters
    scores:  Utility = impact × criticality × (1-risk) × reversibility
    ranks:   branches descending by utility → sequence assigned after sort
    builds:  TMF921 intent with ranked_healing_branches + KPI expressions
    writes:  state["engineer_output"], publishes EVT_ENGINEER_READY

Input:  POST /engineer_event
        { "rca_event": detective.rca.confirmed event dict}

Output: engineer.ready event → ExecutorAgent POST /execute-healing-plan
        tmf921_intent, ranked_healing_branches, execution_order,
        utility_scoring, recovery_targets

Usage:
  python server.py     ← dev (uvicorn reload, port 8081)
  python run.py        ← production (gunicorn uvicorn workers)
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
from app.engineer_agent.agent import engineer_agent   # LlmAgent, name="EngineerAgent"

from app.events import (
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

    rca_event:    detective.rca.confirmed event dict (required)
                  Full event wrapper: { event_id, event_type, payload: {...} }
                  payload must contain: root_cause, domain, affected_entities,
                  risk_score, reversibility_score, impact_score, criticality_score,
                  suggested_remediation, recovery_targets, causal_parameters,
                  change_request_id, hypothesis_id, confidence_score.

    reflex_event: reflex.triage.ready event dict (optional)
                  Carries priority_flag, priority, impact_score from ReflexAgent.
                  Passes latest_gnn_result for criticality_label fallback.
                  If absent: defaults read from rca_event payload directly.
    """
    rca_event:    Optional[Dict[str, Any]] = Field(None, description="detective.rca.confirmed event")
    reflex_event: Optional[Dict[str, Any]] = Field(None, description="reflex.triage.ready event (optional)")

    class Config:
        extra = "allow"


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

    Example request:
    {
      "rca_event": {
        "event_id": "evt-det-001",
        "event_type": "detective.rca.confirmed",
        "payload": {
          "eventId": "EV-dekfn_efjnf_fefe",
          "root_cause": "antenna_tilt_misconfiguration",
          "domain": "RAN",
          "affected_entities": ["eNB-SYN-003", "eNB-SYN-004", "eNB-SYN-005"],
          "risk_score": 0.4,
          "reversibility_score": 0.95,
          "impact_score": 0.94,
          "criticality_score": 1.0,
          "criticality_label": "CRITICAL",
          "confidence_score": 0.85,
          "change_request_id": "CR-SYN-002",
          "hypothesis_id": "RCH-001",
          "suggested_remediation": [...],
          "recovery_targets": [...],
          "causal_parameters": {"parameter": "antenna_tilt_degrees",
                                 "previous_value": 47.1, "current_value": 42.1}
        }
      },
      "reflex_event": null
    }
    """
    request_dict = request.model_dump(exclude_none=True)
    rca_event    = request.rca_event
    reflex_event = request.reflex_event

    if not rca_event:
        rca_payload = request_dict.get("payload") or {
            k: v for k, v in request_dict.items()
            if k not in {"reflex_event", "rca_event"}
        }
        rca_event = {
            "event_id": rca_payload.get("event_id") or str(uuid.uuid4()),
            "event_type": EVT_DETECTIVE_RCA_CONFIRMED,
            "source": "DetectiveAgent",
            "payload": rca_payload,
        }

    rca_payload  = rca_event.get("payload", {})

    logger.info(
        f"[/engineer_event] eventId={rca_payload.get('eventId')} "
        f"root_cause={rca_payload.get('root_cause')} "
        f"domain={rca_payload.get('domain')}"
    )

    # ── Seed session state ────────────────────────────────────────────────────
    # consume_latest(state, EVT_DETECTIVE_RCA_CONFIRMED) in generate_healing_plan
    # scans EVENT_BUS_KEY for event_type == EVT_DETECTIVE_RCA_CONFIRMED.
    # We seed both the bus and the latest_key so it is found regardless of
    # which lookup path consume_latest uses.
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
    # reflexagent_service:
    #   ctx = InvocationContext(session, session_service,
    #                           invocation_id=uuid, agent=reflex_agent)
    #   async for evt in reflex_agent._run_async_impl(ctx): ...
    # engineer:
    #   ctx = InvocationContext(session, session_service,
    #                           invocation_id=uuid, agent=engineer_agent)
    #   async for evt in engineer_agent._run_async_impl(ctx): ...
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

    # ── Build response ────────────────────────────────────────────────────────
    # All keys verified against generate_healing_plan engineer_output dict:
    #   "eventId"                 → rca["eventId"]
    #   "intent_type"             → "TMF921_RCD_CONFIRMED_CAUSE"
    #   "tmf921_intent"           → full TMF921 intent with ranked_healing_branches
    #   "root_cause"              → rca["original_root_cause"]
    #   "root_cause_mapped"       → rca["normalized_root_cause"]
    #   "domain"                  → rca["domain"]
    #   "priority"                → utility_priority (from top branch utility)
    #   "ranked_healing_branches" → sorted by utility, sequence assigned after sort
    #   "execution_order"         → [{sequence, domain, action, action_detail, utility_score}]
    #   "utility_scoring"         → {top_utility_score, kpi_delta_pct, impact_score, ...}
    #   "recovery_targets"        → from Detective Agent PERFORMANCE.csv
    #   "change_request_id"       → from Detective Agent CHANGEREQUEST.csv
    #   "hypothesis_id"           → from Detective Agent
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
            "affected_hex_bins":     engineer_output.get("affected_hex_bins", []),
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
    port = int(os.environ.get("PORT", 8081))
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        log_level="info",
    )
