"""
services/solution_planning_service.py
=======================================
FastAPI service for SolutionPlanningAgent — Stage 7.

Receives investigation.rca.confirmed event via POST /event.
Runs SolutionPlanningAgent.
Returns solution.plan.ready event for ExecutionAgent.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Any

from app.events import (
    EVT_INVESTIGATION_RCA_CONFIRMED,
    EVENT_BUS_KEY,
    NETWORK_STATUS_KEY,
    latest_key,
)
from app.agents.solution_planning_agent.tools import generate_healing_plan

app = FastAPI(title="SolutionPlanningAgent", version="1.0")


class RcaEventRequest(BaseModel):
    rca_event: dict[str, Any]


class _Ctx:
    def __init__(self, state: dict):
        self.state = state


@app.get("/health")
def health():
    return {"status": "ok", "agent": "SolutionPlanningAgent"}


@app.post("/event")
def handle_event(request: RcaEventRequest):
    """
    Receive investigation.rca.confirmed event dict, run SolutionPlanningAgent,
    return solution.plan.ready event.
    """
    rca_event = request.rca_event

    state = {
        NETWORK_STATUS_KEY:                          "HEALING",
        EVENT_BUS_KEY:                               [rca_event],
        latest_key(EVT_INVESTIGATION_RCA_CONFIRMED): rca_event,
    }

    ctx    = _Ctx(state)
    result = generate_healing_plan(ctx)

    if result.get("status") == "IDLE":
        raise HTTPException(status_code=400, detail="SolutionPlanningAgent returned IDLE")

    plan_event = state.get(latest_key("solution.plan.ready"), {})

    return {
        "status":       "processed",
        "agent":        "SolutionPlanningAgent",
        "output_event": plan_event,
        "tool_result":  result,
    }
