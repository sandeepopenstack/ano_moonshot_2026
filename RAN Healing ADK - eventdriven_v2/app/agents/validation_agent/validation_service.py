"""
services/validation_service.py
================================
FastAPI service for ValidationAgent — Stage 10.

Receives execution.completed event via POST /event.
Runs ValidationAgent.
Returns validation.result event.
"""

import sys
import os

from panel import state

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Any

from app.events import (
    EVT_EXECUTION_COMPLETED,
    EVT_GNN_ANOMALY_DETECTED,
    EVENT_BUS_KEY,
    EVT_VALIDATION_RESULT,
    NETWORK_STATUS_KEY,
    latest_key,
)
from app.agents.validation_agent.tools import validate_remediation

app = FastAPI(title="ValidationAgent", version="1.0")

class ExecEventRequest(BaseModel):
    exec_event:         dict[str, Any]          # full execution.completed event dict
    original_gnn_event: dict[str, Any] | None = None



class _Ctx:
    def __init__(self, state: dict):
        self.state = state


@app.get("/health")
def health():
    return {"status": "ok", "agent": "ValidationAgent"}


@app.post("/event")
def handle_event(request: ExecEventRequest):
    """
    Receive execution.completed event, run ValidationAgent.
    original_gnn_event is optional — if provided, enables pre/post Z-score comparison.
    """
    exec_event = request.exec_event

    state = {
        NETWORK_STATUS_KEY:                "HEALING",
        EVENT_BUS_KEY:                     [exec_event],
        latest_key(EVT_EXECUTION_COMPLETED): exec_event,
    }

    # Pre-Z score comparison: restore original GNN event into state
    if request.original_gnn_event:
        state[latest_key(EVT_GNN_ANOMALY_DETECTED)] = request.original_gnn_event

    ctx    = _Ctx(state)
    result = validate_remediation(ctx)

    if result.get("status") == "IDLE":
        raise HTTPException(status_code=400, detail="ValidationAgent returned IDLE")

    from app.events import EVT_VALIDATION_RESULT
    validation_event = state.get(latest_key(EVT_VALIDATION_RESULT), {})

    return {
        "status":          "processed",
        "agent":           "ValidationAgent",
        "output_event":    validation_event,
        "tool_result":     result,
        "resolved":        result.get("resolved"),
        "network_status":  state.get(NETWORK_STATUS_KEY),
    }
