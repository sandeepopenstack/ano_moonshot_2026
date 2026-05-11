"""
services/engineer_service.py
==============================
FastAPI service for EngineerAgent.

Receives detective.rca.confirmed event via POST /engineer_event.
Runs EngineerAgent (generate_healing_plan).
Returns engineer.ready event for ExecutorAgent.

FIXES vs original:
  - Fixed import path: runner.app.agents.engineer_agent.tools
  - Removed duplicate EVT_ENGINEER_READY import
  - generate_healing_plan takes tool_context keyword arg — use _Ctx correctly
  - Priority injection from reflex_event preserved
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Any

from app.events import (
    EVT_DETECTIVE_RCA_CONFIRMED,
    EVT_ENGINEER_READY,
    EVT_REFLEX_TRIAGE_READY,
    EVENT_BUS_KEY,
    NETWORK_STATUS_KEY,
    latest_key,
)
from app.agents.engineer_agent.tools import generate_healing_plan

app = FastAPI(title="EngineerAgent", version="1.0")


class RcaEventRequest(BaseModel):
    rca_event:    dict[str, Any]
    reflex_event: dict[str, Any] | None = None


class _Ctx:
    """Minimal ToolContext duck-type for service use (no ADK Runner needed)."""
    def __init__(self, state: dict):
        self.state = state


@app.get("/health")
def health():
    return {"status": "ok", "agent": "EngineerAgent"}


@app.post("/engineer_event")
def handle_event(request: RcaEventRequest):
    """
    Receives detective.rca.confirmed event.
    Runs EngineerAgent to build TMF921 remediation intent + utility-ranked branches.
    Returns engineer.ready event for ExecutorAgent.

    Optionally pass reflex_event so priority (CRITICAL/HIGH/MEDIUM)
    set by ReflexAgent is carried through. Defaults to CRITICAL if absent.
    """
    rca_event = request.rca_event

    state: dict[str, Any] = {
        NETWORK_STATUS_KEY:                          "HEALING",
        EVENT_BUS_KEY:                               [rca_event],
        latest_key(EVT_DETECTIVE_RCA_CONFIRMED): rca_event,
    }

    # Inject ReflexAgent priority into state so generate_healing_plan can read it
    if request.reflex_event:
        reflex_payload = request.reflex_event.get("payload", {})
        state["request_priority"]  = reflex_payload.get("priority", "CRITICAL")
        state["priority_external"] = reflex_payload.get("priority_external", "CRITICAL")
        state["priority_flag"]     = reflex_payload.get("priority_flag", "P1")
        state["reflex_output"]     = request.reflex_event
    else:
        # Fallback: read from rca_event payload if investigation team includes it
        state["request_priority"] = rca_event.get("payload", {}).get("priority", "CRITICAL")

    ctx = _Ctx(state)
    result = generate_healing_plan(tool_context=ctx)

    if result.get("status") == "IDLE":
        raise HTTPException(
            status_code=400,
            detail="EngineerAgent returned IDLE — no RCA event found in state",
        )

    if result.get("status") == "SKIPPED":
        raise HTTPException(status_code=409, detail="Event already processed")

    engineer_event = state.get(latest_key(EVT_ENGINEER_READY), {})

    return {
        "status":       "processed",
        "agent":        "EngineerAgent",
        "output_event": engineer_event,
        "tool_result":  result,
    }