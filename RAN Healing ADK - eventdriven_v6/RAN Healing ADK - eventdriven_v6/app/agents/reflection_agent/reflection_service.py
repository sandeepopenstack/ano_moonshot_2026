"""
services/reflection_service.py
================================
FastAPI service for ReflectionAgent.

Receives execution.completed event via POST /reflection_event.
Runs check_execution_result → evaluate_resolution in sequence.
Returns reflection.result event (resolved or retrigger).

FIXES vs original:
  - Fixed import path: runner.app.events (not app.events)
  - Fixed import path: runner.app.agents.reflection_agent.tools
  - Replaced non-existent reflection_remediation with real two-tool sequence:
    check_execution_result + evaluate_resolution
  - original_gnn_event injection preserved for pre/post Z-score comparison
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Any

from app.events import (
    EVT_EXECUTION_COMPLETED,
    EVT_GNN_ANOMALY_DETECTED,
    EVT_REFLECTION_RESULT,
    EVENT_BUS_KEY,
    NETWORK_STATUS_KEY,
    latest_key,
)
from app.agents.reflection_agent.tools import (
    check_execution_result,
    evaluate_resolution,
)

app = FastAPI(title="ReflectionAgent", version="1.0")


class ExecEventRequest(BaseModel):
    exec_event:          dict[str, Any]
    original_gnn_event:  dict[str, Any] | None = None   # enables pre/post Z-score comparison


class _Ctx:
    """Minimal ToolContext duck-type for service use (no ADK Runner needed)."""
    def __init__(self, state: dict):
        self.state = state


@app.get("/health")
def health():
    return {"status": "ok", "agent": "ReflectionAgent"}


@app.post("/reflection_event")
def handle_event(request: ExecEventRequest):
    """
    Receives execution.completed event.
    Runs check_execution_result → evaluate_resolution.
    Returns reflection.result — resolved=True (IMO_COMPLIES) or resolved=False (RETRIGGER).

    Pass original_gnn_event in request body to enable pre/post Z-score comparison.
    Optional — reflection works without it (pre_z defaults to 0.0).
    """
    exec_event = request.exec_event

    local_state: dict[str, Any] = {
        NETWORK_STATUS_KEY:                  "HEALING",
        EVENT_BUS_KEY:                       [exec_event],
        latest_key(EVT_EXECUTION_COMPLETED): exec_event,
    }

    # Restore original GNN event for pre/post Z-score comparison (slide 10)
    if request.original_gnn_event:
        local_state[latest_key(EVT_GNN_ANOMALY_DETECTED)] = request.original_gnn_event

    ctx = _Ctx(local_state)

    # Step 1 — parse execution output
    exec_result = check_execution_result(tool_context=ctx)

    if exec_result.get("status") == "IDLE":
        raise HTTPException(
            status_code=400,
            detail="ReflectionAgent: no execution.completed event found",
        )

    if exec_result.get("status") == "SKIPPED":
        raise HTTPException(status_code=409, detail="Event already processed")

    # Step 2 — evaluate resolution, publish reflection.result
    reflection_result = evaluate_resolution(tool_context=ctx, exec_result=exec_result)

    reflection_event = local_state.get(latest_key(EVT_REFLECTION_RESULT), {})

    return {
        "status":         "processed",
        "agent":          "ReflectionAgent",
        "output_event":   reflection_event,
        "tool_result":    reflection_result,
        "resolved":       reflection_result.get("resolved"),
        "network_status": local_state.get(NETWORK_STATUS_KEY),
    }