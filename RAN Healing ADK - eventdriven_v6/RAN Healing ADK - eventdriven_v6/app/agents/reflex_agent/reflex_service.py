"""
services/reflex_service.py
============================
FastAPI service for ReflexAgent.

Receives GNN anomaly event via POST /reflex_event.
Runs fetch_gnn_inference → perform_triage in sequence.

IMPORTANT — two payloads kept separate:
  external_payload → returned to Investigation team (4 fields only, per contract)
  full session state → kept internally for EngineerAgent (all fields)

The Investigation team (Ericsson) explicitly expects ONLY:
  { entity_ids, domain_triage, priority, reference_time }
Sending extra fields is a contract violation.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Any

from app.events import (
    EVT_GNN_ANOMALY_DETECTED,
    EVT_REFLEX_TRIAGE_READY,
    EVENT_BUS_KEY,
    NETWORK_STATUS_KEY,
    latest_key,
    make_gnn_anomaly_event,
)
from app.agents.reflex_agent.tools import fetch_gnn_inference, perform_triage

app = FastAPI(title="ReflexAgent", version="1.0")


class GnnEventRequest(BaseModel):
    gnn_event: dict[str, Any]


class _Ctx:
    """Minimal ToolContext duck-type for service use (no ADK Runner needed)."""
    def __init__(self, state: dict):
        self.state = state


@app.get("/health")
def health():
    return {"status": "ok", "agent": "ReflexAgent"}


@app.post("/reflex_event")
def handle_event(request: GnnEventRequest):
    """
    Receives raw GNN payload dict.
    Runs fetch_gnn_inference → perform_triage.

    Returns to Investigation team:
      external_payload — exactly 4 fields per contract
      { entity_ids, domain_triage, priority, reference_time }

    Full triage output is preserved internally in session state
    for EngineerAgent (ranked_branches, execution_order, etc.).
    """
    raw_payload = request.gnn_event

    if raw_payload.get("event_type") == EVT_GNN_ANOMALY_DETECTED:
        gnn_event = raw_payload
    else:
        gnn_event = make_gnn_anomaly_event(raw_payload)

    state: dict[str, Any] = {
        NETWORK_STATUS_KEY:                   "ANOMALY_DETECTED",
        EVENT_BUS_KEY:                        [gnn_event],
        latest_key(EVT_GNN_ANOMALY_DETECTED): gnn_event,
    }

    ctx = _Ctx(state)

    # Step 1 — GNN inference
    gnn_result = fetch_gnn_inference(tool_context=ctx)
    if not gnn_result or "anomalousSubgraph" not in gnn_result:
        raise HTTPException(
            status_code=502,
            detail="GNN inference returned no anomalous subgraph",
        )

    # Step 2 — domain triage
    triage_result = perform_triage(tool_context=ctx, gnn_inference=gnn_result)

    if triage_result.get("status") == "BELOW_THRESHOLD":
        raise HTTPException(
            status_code=400,
            detail=f"ReflexAgent: score below threshold — "
                   f"score={triage_result.get('composite_score')}",
        )
    if triage_result.get("status") == "SKIPPED":
        raise HTTPException(status_code=409, detail="Event already processed")

    # ── External payload — exactly 4 fields for Investigation team ─────────
    external_payload = {
        "entity_ids":     triage_result.get("entity_ids"),
        "domain_triage":  triage_result.get("domain_triage"),
        "priority":       triage_result.get("priority"),
        "reference_time": triage_result.get("reference_time"),
    }

    # Full reflex event (kept for internal pipeline / Engineer service)
    reflex_event = state.get(latest_key(EVT_REFLEX_TRIAGE_READY), {})

    return {
        "status":           "processed",
        "agent":            "ReflexAgent",
        # Investigation team reads this
        "investigation_input": external_payload,
        # Internal use — pass to engineer_service as reflex_event
        "output_event":    reflex_event,
    }