# import os, sys, uvicorn
# from uuid import uuid4
# from google.adk.cli.fast_api import get_fast_api_app

# sys.path.insert(0, os.path.dirname(__file__))

# from ran_healing_shared.events import (
#     NETWORK_STATUS_KEY, EVENT_BUS_KEY, latest_key,
#     EVT_EXECUTION_COMPLETED,
# )
# from reflection_agent.tools import check_execution_result, evaluate_and_publish

# parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# app = get_fast_api_app(agents_dir=parent_dir, web=False, a2a=True)


# class _Ctx:
#     def __init__(self, state):
#         self.state = state


# @app.post("/api/reflection")
# def reflection_invoke(payload: dict):
#     exec_event = {
#         "event_id": str(uuid4()),
#         "event_type": EVT_EXECUTION_COMPLETED,
#         "source": "ExecutorAgent",
#         "payload": payload,
#         "network_status": "HEALING",
#     }
#     state = {
#         NETWORK_STATUS_KEY: "HEALING",
#         EVENT_BUS_KEY: [],
#         latest_key(EVT_EXECUTION_COMPLETED): exec_event,
#     }
#     ctx = _Ctx(state)
#     check_execution_result(ctx)
#     result = evaluate_and_publish(ctx)
#     reflection_output = state.get("reflection_output", {})
#     return {
#         "status": result[:50] if result else "ok",
#         "resolved": reflection_output.get("resolved"),
#         "reflection_status": reflection_output.get("status"),
#         "reflection_output": reflection_output,
#         "network_status": state.get(NETWORK_STATUS_KEY),
#     }


# if __name__ == "__main__":
#     uvicorn.run(app, host="0.0.0.0", port=8000)

"""
server.py — ReflectionAgent Deployment Server
===============================================
FastAPI deployment service for ReflectionAgent.
"""

import os
import sys
import logging
from typing import Any, Dict, Optional

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

# ── Project imports — verified against tools.py ───────────────────────────────
from tools import (
    check_execution_result,   # Tool 1: parse executor output → state["reflection_exec_result"]
    evaluate_and_publish,     # Tool 2: GNN re-run + z-score + KPI → publish IMO_COMPLIES
)

from ran_healing_shared.events import (
    EVT_EXECUTION_COMPLETED,    # tools.py: consume_latest reads this
    EVT_REFLECTION_RESULT,      # tools.py: make_reflection_result_event writes this
    EVENT_BUS_KEY,
    NETWORK_STATUS_KEY,
    latest_key,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="ReflectionAgent Service — RAN Self-Healing",
    description=(
        "Step 10. Receives execution.completed event, "
        "validates post-remediation state (GNN z-score + KPI recovery), "
        "publishes IMO_COMPLIES (RESOLVED) or RETRIGGER_INVESTIGATION."
    ),
    version="1.0.0",
)


# ── _Ctx duck-type ────────────────────────────────────────────────────────────
# Both tools only use tool_context.state → duck-type is sufficient.
# agent.py explicitly says NOT to use LlmAgent runner for this agent.
class _Ctx:
    """Minimal ToolContext duck-type. Both reflection tools only use .state."""
    def __init__(self, state: dict):
        self.state = state


# ── Pydantic input models ─────────────────────────────────────────────────────

class ExecEventRequest(BaseModel):
    """
    Request body for POST /reflection_event.
    """
    exec_event:          Dict[str, Any]           = Field(...,  description="execution.completed event")
    pre_action_z_score:  Optional[float]          = Field(None, description="Pre-action z-score from ReflexAgent (recommended)")
    original_gnn_event:  Optional[Dict[str, Any]] = Field(None, description="Original GNN event for pre-z fallback (optional)")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Liveness probe for Cloud Run / Kubernetes."""
    return {"status": "ok", "agent": "ReflectionAgent"}


@app.get("/")
async def root():
    return {
        "agent":    "ReflectionAgent",
        "version":  "1.0.0",
        "endpoint": "POST /reflection_event",
        "input":    "Step 9 — execution.completed event",
        "output":   "Step 10 — reflection.result (IMO_COMPLIES / RETRIGGER_INVESTIGATION)",
        "tools":    "check_execution_result → evaluate_and_publish (direct, no LLM)",
    }


@app.post("/reflection_event")
async def reflection_event(request: ExecEventRequest):
    """
    Step 9 → Step 10 — ReflectionAgent validation.

    Runs two tools in strict sequence (deterministic, no LLM):
      1. check_execution_result  → parses TMF641/TMF921 execution output
      2. evaluate_and_publish    → GNN re-run + z-score + KPI → IMO_COMPLIES
    """
    exec_event = request.exec_event
    logger.info(
        f"[/reflection_event] activation_id="
        f"{exec_event.get('payload', {}).get('activation_id')} "
        f"success={exec_event.get('payload', {}).get('success')}"
    )

    # ── Seed session state ────────────────────────────────────────────────────
    # consume_latest(state, EVT_EXECUTION_COMPLETED) in check_execution_result
    # scans EVENT_BUS_KEY for event_type == EVT_EXECUTION_COMPLETED.
    # Seed both the bus and latest_key so it is found regardless of lookup path.
    state: Dict[str, Any] = {
        NETWORK_STATUS_KEY:                       "HEALING",
        EVENT_BUS_KEY:                            [exec_event],
        latest_key(EVT_EXECUTION_COMPLETED):      exec_event,
    }

    # ── Inject pre-action z-score ─────────────────────────────────────────────
    # evaluate_and_publish reads: state.get("pre_action_z_score")
    # Priority: explicit field > original_gnn_event.anomalyScore > 0.0 fallback
    if request.pre_action_z_score is not None:
        state["pre_action_z_score"] = request.pre_action_z_score
    elif request.original_gnn_event:
        gnn_score = (
            request.original_gnn_event.get("anomalyScore", {}).get("compositeScore")
            or request.original_gnn_event.get("anomalyScore", {}).get("zScore")
        )
        if gnn_score:
            state["pre_action_z_score"] = float(gnn_score)

    # ── Run tools directly via _Ctx ───────────────────────────────────────────
    # agent.py: "NOT called via LlmAgent.run_async() — tools called DIRECTLY"
    # Tool 1 → Tool 2 in strict sequence, no LLM reasoning between them.
    ctx = _Ctx(state)

    # Tool 1: check_execution_result
    # Returns string: "CHECK_COMPLETE: ..." / "CHECK_ERROR: ..." / "CHECK_SKIPPED: ..."
    check_result_str = check_execution_result(tool_context=ctx)

    if "CHECK_ERROR" in check_result_str:
        logger.error(f"[/reflection_event] {check_result_str}")
        return JSONResponse(
            status_code=400,
            content={"error": check_result_str},
        )

    if "CHECK_SKIPPED" in check_result_str:
        logger.warning(f"[/reflection_event] {check_result_str}")
        return JSONResponse(
            status_code=409,
            content={"error": "Event already processed", "detail": check_result_str},
        )

    # Tool 2: evaluate_and_publish
    # Returns string: "RESOLVED: ..." / "RETRIGGER: ..." / "FAILED_AFTER_RETRIES: ..."
    eval_result_str = evaluate_and_publish(tool_context=ctx)

    # ── Read output from state ────────────────────────────────────────────────
    # evaluate_and_publish writes state["reflection_output"] and publishes EVT_REFLECTION_RESULT
    reflection_output  = state.get("reflection_output", {})
    reflection_event   = state.get(latest_key(EVT_REFLECTION_RESULT), {})
    network_status     = state.get(NETWORK_STATUS_KEY, "UNKNOWN")

    if not reflection_output:
        logger.error("[/reflection_event] No reflection_output in state after tool run")
        return JSONResponse(
            status_code=500,
            content={"error": "ReflectionAgent produced no output"},
        )

    # ── Build response ────────────────────────────────────────────────────────
    return JSONResponse(content={
        "status":         "processed",
        "agent":          "ReflectionAgent",
        # Full published reflection.result event
        "output_event":   reflection_event,
        # Resolution fields
        "resolved":        reflection_output.get("resolved"),
        "network_status":  network_status,
        "imo_status":      reflection_output.get("status"),
        # Validation details
        "zscore_comparison": reflection_output.get("zscore_comparison", {}),
        # GUI dashboard
        "gui_dashboard": {
            "gui_status":     reflection_output.get("gui_status"),
            "business_view":  reflection_output.get("business_view"),
            "service_view":   reflection_output.get("service_view"),
            "gnn_topology":   reflection_output.get("gnn_topology_view"),
            "topology_state": reflection_output.get("topology_state"),
        },
        # TMF metadata
        "tmf_metadata": {
            "activation_id": reflection_output.get("activation_id"),
            "intent_id":     reflection_output.get("intent_id"),
            "domain":        reflection_output.get("domain"),
        },
        # Retrigger info (populated when not resolved)
        "retrigger": {
            "count":   reflection_output.get("retrigger_count", 0),
            "reason":  reflection_output.get("retrigger_reason"),
            "next":    reflection_output.get("next_target"),
        },
        "tool_result": eval_result_str,
    })


# ── Dev entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        log_level="info",
    )