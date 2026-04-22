"""
services/monitoring_service.py
================================
FastAPI service for MonitoringAgent — Stage 5.

Receives GNN anomaly event via POST /event.
Runs MonitoringAgent triage.
Returns monitoring.triage.ready event for InvestigationAgent.

Deploy as standalone Cloud Run service.
Replace mock_api calls with real endpoints.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Any

from app.events import (
    EVT_GNN_ANOMALY_DETECTED,
    EVENT_BUS_KEY,
    NETWORK_STATUS_KEY,
    latest_key,
    make_gnn_anomaly_event,
    publish_event,
)
from app.agents.monitoring_agent.tools import monitor_and_triage

app = FastAPI(title="MonitoringAgent", version="1.0")


class GnnEventRequest(BaseModel):
    gnn_event: dict[str, Any]   # raw GNN payload (NOT pre-wrapped)


class _Ctx:
    def __init__(self, state: dict):
        self.state = state


@app.get("/health")
def health():
    return {"status": "ok", "agent": "MonitoringAgent", "stage": 5}


@app.post("/event")
def handle_event(request: GnnEventRequest):
    """
    Receives raw GNN payload dict.
    Wraps it as a proper domain event (adds event_id, event_type, event_time).
    Runs MonitoringAgent triage.
    Returns monitoring.triage.ready output event.
    """
    raw_payload = request.gnn_event

    # If caller already wrapped (has event_type), use as-is
    # Otherwise wrap it so event_id and event_type are present
    if "event_type" in raw_payload and raw_payload["event_type"] == "gnn.anomaly.detected":
        gnn_event = raw_payload
    else:
        gnn_event = make_gnn_anomaly_event(raw_payload)

    state = {
        NETWORK_STATUS_KEY:                   "ANOMALY_DETECTED",
        EVENT_BUS_KEY:                        [gnn_event],
        latest_key(EVT_GNN_ANOMALY_DETECTED): gnn_event,
    }

    ctx    = _Ctx(state)
    result = monitor_and_triage(ctx)

    if result.get("status") in ("IDLE", "BELOW_THRESHOLD"):
        raise HTTPException(
            status_code=400,
            detail=f"MonitoringAgent: {result.get('status')} — {result.get('reason', result.get('z_score'))}"
        )

    if result.get("status") == "SKIPPED":
        raise HTTPException(status_code=409, detail="Event already processed")

    monitoring_event = state.get(latest_key("monitoring.triage.ready"), {})

    return {
        "status":       "processed",
        "agent":        "MonitoringAgent",
        "stage":        5,
        "output_event": monitoring_event,
        "tool_result":  result,
    }