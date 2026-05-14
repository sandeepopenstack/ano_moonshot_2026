"""
reflection_service.py
=======================
FastAPI service for ReflectionAgent.

Receives execution.completed event via POST /reflection_event.
Runs check_execution_result → evaluate_and_publish in sequence.
Returns reflection.result (IMO_COMPLIES or RETRIGGER_INVESTIGATION).

Mirrors monitoring_agent/server.py and reflexagent_service.py patterns.

Fixes vs reference reflection_service.py:
  FIX 1: check_execution_result returns a STRING (not a dict).
          Reference called exec_result.get("status") → AttributeError on str.
          Fixed: check for "CHECK_ERROR" / "CHECK_SKIPPED" substrings in the string.

  FIX 2: Reference called evaluate_resolution (backward-compat wrapper).
          Direct call to evaluate_and_publish is cleaner and avoids wrapper overhead.
          evaluate_and_publish writes state["reflection_output"] directly.

  FIX 3: pre_action_z_score injection.
          evaluate_and_publish reads state["pre_action_z_score"] for pre/post comparison.
          Injected from original_gnn_event.anomalyScore.compositeScore if provided.
          Without this, pre_z defaults to 0.0 and the comparison is meaningless.

  FIX 4: Import paths use the service-local app.* package.
          Import EVT_REFLECTION_RESULT from app.events.
"""

import os
import sys
import logging
import uuid
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
from app.reflection_agent.tools import (
    check_execution_result,  # Tool 1: parse executor output → state["reflection_exec_result"]
    evaluate_and_publish,    # Tool 2: Spanner/MCP gates -> publish reflection.result
                             # (use directly — not evaluate_resolution wrapper)
)

from app.events import (
    EVT_EXECUTION_COMPLETED,   # tools.py: consume_latest reads this
    EVT_GNN_ANOMALY_DETECTED,  # optional — source of pre_action_z_score fallback
    EVT_REFLECTION_RESULT,     # tools.py: make_reflection_result_event writes this
    EVENT_BUS_KEY,
    NETWORK_STATUS_KEY,
    latest_key,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="ReflectionAgent",
    version="1.0.0",
)


# ── _Ctx duck-type ────────────────────────────────────────────────────────────
# agent.py: "NOT called via LlmAgent.run_async() — tools called DIRECTLY"
# Both tools only use tool_context.state → _Ctx duck-type is sufficient.
class _Ctx:
    """Minimal ToolContext duck-type for service use (no ADK Runner needed)."""
    def __init__(self, state: dict):
        self.state = state


# ── Pydantic input models ─────────────────────────────────────────────────────

class ExecEventRequest(BaseModel):
    """
    Mirrors monitoring_agent/server.py GNNRequest pattern:
      monitoring: { "gnn": GNNEventModel }
      reflection: { "exec_event": dict, "original_gnn_event": dict|None }
    """
    exec_event:          Optional[Dict[str, Any]] = Field(None, description="execution.completed event")
    pre_action_z_score:  Optional[float]          = Field(None, description="Pre-action z-score from ReflexAgent")

    class Config:
        extra = "allow"
    original_gnn_event:  Optional[Dict[str, Any]] = Field(None, description="Original GNN event — enables pre/post Z-score comparison")


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
    }


@app.post("/reflection_event")
async def reflection_event(request: ExecEventRequest):
    """
    Step 9 → Step 10 — ReflectionAgent validation.

    Receives execution.completed event.
    Runs check_execution_result → evaluate_and_publish.
    Returns reflection.result — resolved=True (IMO_COMPLIES) or resolved=False (RETRIGGER).

    Pass original_gnn_event to enable pre/post Z-score comparison.
    Without it: pre_z defaults to 0.0 and comparison may be misleading.

    Example:
    {
      "exec_event": {
        "event_id": "evt-exec-001",
        "event_type": "execution.completed",
        "payload": {
          "success": true,
          "state": "completed",
          "activation_id": "ACT-SYN-002",
          "tmf641_order": {...},
          "tmf921_intent": {"expressions": [...], ...}
        }
      },
      "original_gnn_event": null
    }
    """
    request_dict = request.model_dump(exclude_none=True)
    exec_event = request.exec_event

    if not exec_event:
        exec_payload = request_dict.get("payload") or {
            k: v for k, v in request_dict.items()
            if k not in {"exec_event", "pre_action_z_score", "original_gnn_event"}
        }
        exec_event = {
            "event_id": exec_payload.get("event_id") or str(uuid.uuid4()),
            "event_type": EVT_EXECUTION_COMPLETED,
            "source": "ExecutorAgent",
            "payload": exec_payload,
        }

    logger.info(
        f"[/reflection_event] activation_id="
        f"{exec_event.get('payload', {}).get('activation_id')} "
        f"success={exec_event.get('payload', {}).get('success')}"
    )

    # ── Seed session state ────────────────────────────────────────────────────
    # Mirrors reflection_service.py reference + server.py pattern.
    # consume_latest(state, EVT_EXECUTION_COMPLETED) scans EVENT_BUS_KEY.
    local_state: Dict[str, Any] = {
        NETWORK_STATUS_KEY:                       "HEALING",
        EVENT_BUS_KEY:                            [exec_event],
        latest_key(EVT_EXECUTION_COMPLETED):      exec_event,
    }

    # ── Inject pre-action z-score for pre/post comparison ────────────────────
    # FIX 3: evaluate_and_publish reads state["pre_action_z_score"].
    # Without injection, pre_z = 0.0 → comparison breaks.
    # Source: original_gnn_event.anomalyScore.compositeScore (from ReflexAgent).
    if request.pre_action_z_score is not None:
        local_state["pre_action_z_score"] = request.pre_action_z_score
    elif request.original_gnn_event:
        local_state[latest_key(EVT_GNN_ANOMALY_DETECTED)] = request.original_gnn_event
        gnn_payload = request.original_gnn_event.get("payload", request.original_gnn_event)
        gnn_score = (
            gnn_payload.get("anomalyScore", {}).get("compositeScore")
            or gnn_payload.get("anomalyScore", {}).get("zScore")
        )
        if gnn_score:
            local_state["pre_action_z_score"] = float(gnn_score)

    # ── Run tools directly ────────────────────────────────────────────────────
    ctx = _Ctx(local_state)

    # Tool 1: check_execution_result
    # FIX 1: returns STRING not dict — check substrings, not .get("status")
    check_result_str = check_execution_result(tool_context=ctx)

    if "CHECK_ERROR" in check_result_str:
        logger.error(f"[/reflection_event] Tool 1 error: {check_result_str}")
        return JSONResponse(
            status_code=400,
            content={"error": check_result_str},
        )

    if "CHECK_SKIPPED" in check_result_str:
        return JSONResponse(
            status_code=409,
            content={"error": "Event already processed", "detail": check_result_str},
        )

    # Tool 2: evaluate_and_publish
    # FIX 2: call evaluate_and_publish directly (not evaluate_resolution wrapper)
    eval_result_str = evaluate_and_publish(tool_context=ctx)

    # ── Read output from state ────────────────────────────────────────────────
    reflection_output  = local_state.get("reflection_output", {})
    reflection_event   = local_state.get(latest_key(EVT_REFLECTION_RESULT), {})
    network_status     = local_state.get(NETWORK_STATUS_KEY, "UNKNOWN")

    return JSONResponse(content={
        "status":         "processed",
        "agent":          "ReflectionAgent",
        # Full published reflection.result event — pass to downstream orchestrator
        "output_event":   reflection_event,
        # Tool result string
        "tool_result":    eval_result_str,
        # Key resolution fields
        "resolved":       reflection_output.get("resolved"),
        "network_status": network_status,
        "imo_status":     reflection_output.get("status"),
        # Validation details
        "zscore_comparison": reflection_output.get("zscore_comparison", {}),
        "kpi_validation":    reflection_output.get("kpi_validation", []),
        # GUI dashboard
        "gui_dashboard": {
            "gui_status":     reflection_output.get("gui_status"),
            "business_view":  reflection_output.get("business_view"),
            "service_view":   reflection_output.get("service_view"),
            "gnn_topology":   reflection_output.get("gnn_topology_view"),
            "topology_state": reflection_output.get("topology_state"),
        },
        # Retrigger info (populated when not resolved)
        "retrigger": {
            "count":  reflection_output.get("retrigger_count", 0),
            "reason": reflection_output.get("retrigger_reason"),
            "next":   reflection_output.get("next_target"),
        },
    })


# ── Dev entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8082))
    uvicorn.run(
        "app.reflection_agent.reflection_service:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        log_level="info",
    )
