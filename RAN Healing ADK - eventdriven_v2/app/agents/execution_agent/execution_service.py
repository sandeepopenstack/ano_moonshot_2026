"""
services/execution_service.py
================================
FastAPI service for ExecutionAgent — Stage 9.

Receives solution.plan.ready event via POST /event.
Runs ExecutionAgent mock.
Returns execution.completed event for ValidationAgent.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Any

from app.events import (
    EVT_SOLUTION_PLAN_READY,
    EVENT_BUS_KEY,
    NETWORK_STATUS_KEY,
    latest_key,
)
from app.agents.execution_agent.tools import run_execution_mock

app = FastAPI(title="ExecutionAgent", version="1.0")


class PlanEventRequest(BaseModel):
    plan_event: dict[str, Any]


class _Ctx:
    def __init__(self, state: dict):
        self.state = state


@app.get("/health")
def health():
    return {"status": "ok", "agent": "ExecutionAgent"}


@app.post("/event")
def handle_event(request: PlanEventRequest):
    """
    Receive solution.plan.ready event, run ExecutionAgent,
    return execution.completed event.
    """
    plan_event = request.plan_event

    state = {
        NETWORK_STATUS_KEY:                  "HEALING",
        EVENT_BUS_KEY:                       [plan_event],
        latest_key(EVT_SOLUTION_PLAN_READY): plan_event,
    }

    ctx    = _Ctx(state)
    result = run_execution_mock(ctx)

    if result.get("status") == "IDLE":
        raise HTTPException(status_code=400, detail="ExecutionAgent returned IDLE")

    exec_event = state.get(latest_key("execution.completed"), {})

    return {
        "status":       "processed",
        "agent":        "ExecutionAgent",
        "output_event": exec_event,
        "tool_result":  result,
    }
