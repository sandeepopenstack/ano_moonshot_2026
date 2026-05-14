"""
engineeragent_service.py
==========================
FastAPI deployment service for EngineerAgent.

Mirrors engineer_service.py pattern exactly:
  Input:  POST /engineer_event
          { "rca_event": detective.rca.confirmed event dict,
            "reflex_event": optional reflex.triage.ready event dict }
  Runs:   generate_healing_plan (single tool, _Ctx duck-type)
  Output: engineer.ready event → ExecutorAgent

Why _Ctx duck-type (not InvocationContext like ReflexAgent)?
  generate_healing_plan is a single tool that only uses tool_context.state.
  No LLM reasoning needed — the tool is deterministic.
  _Ctx provides state without needing ADK Runner or LlmAgent._run_async_impl.
  This matches the engineer_service.py reference pattern exactly.

EngineerAgent flow (Step 7):
  generate_healing_plan:
    reads:   consume_latest(state, EVT_DETECTIVE_RCA_CONFIRMED)
    parses:  root_cause, affected_entities, risk_score, reversibility_score,
             suggested_remediation, recovery_targets, causal_parameters
    scores:  Utility = impact × criticality × (1-risk) × reversibility
    ranks:   branches by utility score → sequence 1 = highest = Executor first
    builds:  TMF921 intent with ranked_healing_branches + expressions
    writes:  state["engineer_output"], publishes EVT_ENGINEER_READY

Output → ExecutorAgent POST /execute-healing-plan:
  tmf921_intent with ranked_healing_branches, expressions (KPI targets),
  execution_order, utility_scoring, change_request_id, hypothesis_id
"""

import os
import sys
import uuid
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

# ── Project imports — verified against tools.py and agent.py ─────────────────
from app.engineer_agent.tools import generate_healing_plan  # single tool

from app.events import (
    EVT_DETECTIVE_RCA_CONFIRMED,  # tools.py: consume_latest reads this
    EVT_ENGINEER_READY,           # tools.py: make_engineer_event writes this
    EVT_REFLEX_TRIAGE_READY,      # optional — carries ReflexAgent priority
    EVENT_BUS_KEY,
    NETWORK_STATUS_KEY,
    latest_key,
)

import logging
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


# ── _Ctx duck-type ────────────────────────────────────────────────────────────
# Mirrors engineer_service.py _Ctx exactly.
# generate_healing_plan only uses tool_context.state → duck-type is sufficient.
# No InvocationContext or ADK Runner needed for single deterministic tool.
class _Ctx:
    """Minimal ToolContext duck-type. generate_healing_plan only uses .state."""
    def __init__(self, state: dict):
        self.state = state


# ── Pydantic input models ─────────────────────────────────────────────────────
# Mirrors engineer_service.py RcaEventRequest exactly.

class RcaEventRequest(BaseModel):
    """
    Request body for POST /engineer_event.

    rca_event:    detective.rca.confirmed event dict (required)
                  Must contain payload with root_cause, affected_entities,
                  risk_score, reversibility_score, suggested_remediation,
                  recovery_targets, causal_parameters, change_request_id.

    reflex_event: reflex.triage.ready event dict (optional)
                  Carries priority_flag, priority, impact_score from ReflexAgent.
                  If absent: defaults to CRITICAL priority from rca_event payload.
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
    }


@app.post("/engineer_event")
async def engineer_event(request: RcaEventRequest):
    """
    Step 6 → Step 7 — EngineerAgent healing plan generation.

    Runs generate_healing_plan tool:
      1. Parses detective.rca.confirmed payload
      2. Fetches business metadata (BigQuery or structural fallback)
      3. Scores branches: Utility = impact × criticality × (1-risk) × reversibility
      4. Ranks by utility (Sequence 1 = highest = Executor runs first)
      5. Builds TMF921 intent with ranked_healing_branches + KPI expressions
      6. Publishes engineer.ready event

    Returns engineer.ready event for ExecutorAgent POST /execute-healing-plan.

    Example request:
    {
      "rca_event": {
        "event_id": "evt-001",
        "event_type": "detective.rca.confirmed",
        "payload": {
          "eventId": "EV-001",
          "root_cause": "antenna_tilt_misconfiguration",
          "domain": "RAN",
          "affected_entities": ["eNB-SYN-003", "eNB-SYN-004"],
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
          "causal_parameters": {...}
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

    rca_payload = rca_event.get("payload", {})
    logger.info(
        f"[/engineer_event] eventId={rca_payload.get('eventId')} "
        f"root_cause={rca_payload.get('root_cause')} "
        f"domain={rca_payload.get('domain')}"
    )

    # ── Seed session state ────────────────────────────────────────────────────
    # Mirrors engineer_service.py exactly:
    #   state[latest_key(EVT_DETECTIVE_RCA_CONFIRMED)] = rca_event
    #   state[NETWORK_STATUS_KEY] = "HEALING"
    #   state[EVENT_BUS_KEY] = [rca_event]
    #
    # consume_latest(state, EVT_DETECTIVE_RCA_CONFIRMED) in generate_healing_plan
    # reads from EVENT_BUS_KEY looking for event_type == EVT_DETECTIVE_RCA_CONFIRMED
    state: Dict[str, Any] = {
        NETWORK_STATUS_KEY:                          "HEALING",
        EVENT_BUS_KEY:                               [rca_event],
        latest_key(EVT_DETECTIVE_RCA_CONFIRMED):     rca_event,
    }

    # ── Inject ReflexAgent priority into state ────────────────────────────────
    # Mirrors engineer_service.py priority injection exactly.
    # generate_healing_plan reads priority from state for utility priority label.
    if reflex_event:
        reflex_payload = reflex_event.get("payload", {})
        state["request_priority"]  = reflex_payload.get("priority", "CRITICAL")
        state["priority_external"] = reflex_payload.get("priority_external", "CRITICAL")
        state["priority_flag"]     = reflex_payload.get("priority_flag", "P1")
        state["reflex_output"]     = reflex_event
        # Also carry GNN result if present (criticality_label for utility formula)
        if reflex_payload.get("latest_gnn_result"):
            state["latest_gnn_result"] = reflex_payload["latest_gnn_result"]
    else:
        # Fallback: read from rca_event payload directly
        state["request_priority"] = rca_payload.get("priority", "CRITICAL")

    # ── Run generate_healing_plan via _Ctx duck-type ──────────────────────────
    # Single deterministic tool — no LLM reasoning, no ADK Runner needed.
    # Mirrors engineer_service.py: ctx = _Ctx(state); generate_healing_plan(ctx)
    ctx    = _Ctx(state)
    result = generate_healing_plan(tool_context=ctx)

    # ── Handle tool return statuses ───────────────────────────────────────────
    if result.get("status") == "IDLE":
        logger.error("[/engineer_event] generate_healing_plan returned IDLE")
        return JSONResponse(
            status_code=400,
            content={"error": "EngineerAgent IDLE — no detective.rca.confirmed event found in state"},
        )

    if result.get("status") == "SKIPPED":
        logger.warning("[/engineer_event] Event already processed")
        return JSONResponse(
            status_code=409,
            content={"error": "Event already processed", "event_id": result.get("event_id")},
        )

    # ── Read output from state ────────────────────────────────────────────────
    # engineer_event = the full published event (for passing to ExecutorAgent)
    # engineer_output = the raw output dict (all fields for debugging)
    # Mirrors engineer_service.py:
    #   engineer_event = state.get(latest_key(EVT_ENGINEER_READY), {})
    engineer_event_out = state.get(latest_key(EVT_ENGINEER_READY), {})
    engineer_output    = state.get("engineer_output", {})

    if not engineer_event_out and not engineer_output:
        logger.error("[/engineer_event] No engineer output written to state")
        return JSONResponse(
            status_code=500,
            content={"error": "EngineerAgent produced no output in state"},
        )

    # ── Return response ───────────────────────────────────────────────────────
    # Mirrors engineer_service.py return exactly:
    #   status, agent, output_event, tool_result
    # ExecutorAgent reads: output_event["payload"]["tmf921_intent"]
    return JSONResponse(content={
        "status":       "processed",
        "agent":        "EngineerAgent",
        # Full published engineer.ready event — pass this to ExecutorAgent
        "output_event": engineer_event_out,
        # Raw tool result dict — for logging/debugging
        "tool_result":  result,
        # Convenience fields for ExecutorAgent
        "intent_id":     result.get("intent_id"),
        "root_cause":    result.get("root_cause"),
        "root_cause_mapped": result.get("root_cause_mapped"),
        "affected_hex_bins": engineer_output.get("affected_hex_bins", []),
        "branch_count":  result.get("branch_count"),
        "top_utility_score": engineer_output.get("utility_scoring", {}).get("top_utility_score"),
        "execution_order": result.get("execution_order", []),
    })


# ── Dev entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8081))
    uvicorn.run(
        "app.engineer_agent.engineer_service:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        log_level="info",
    )
