"""
server.py — ReflectionAgent Deployment Server
===============================================
FastAPI deployment service for ReflectionAgent.

IMPORTANT — Why _Ctx (not InvocationContext like ReflexAgent/EngineerAgent):
  agent.py explicitly states:
    "This agent is NOT called via LlmAgent.run_async() in root_agent.
     root_agent calls its tools DIRECTLY (deterministic logic,
     no LLM reasoning needed, and LLM path caused timeout hangs)."
  Therefore server.py uses _Ctx duck-type + direct sequential tool calls,
  NOT InvocationContext + _run_async_impl.

ReflectionAgent flow (Step 10):
  Tool 1: check_execution_result (NO args)
    reads:   consume_latest(state, EVT_EXECUTION_COMPLETED)
    parses:  success, state, activation_id, intent_id, action_type,
             tmf641_order.service_characteristics, tmf921_intent.expressions
    writes:  state["reflection_exec_result"]
    returns: string "CHECK_COMPLETE: ..." or "CHECK_ERROR: ..."

  Tool 2: evaluate_and_publish (NO args)
    reads:   state["reflection_exec_result"]
             Spanner anomaly/performance rows via MCP
    validates: Gate 1 execution result, Gate 2 anomaly_label, Gate 3 performance
    writes:  state["reflection_output"]
             state[NETWORK_STATUS_KEY] = "RESOLVED" or "ANOMALY_DETECTED"
    publishes: EVT_REFLECTION_RESULT (IMO_COMPLIES or RETRIGGER_INVESTIGATION)
    returns: string "RESOLVED: ..." or "RETRIGGER: ..."

Input:  POST /reflection_event
        { "exec_event": execution.completed event dict,
          "pre_action_z_score": float (optional, from ReflexAgent),
          "original_gnn_event": dict (optional, for pre-z fallback) }

Output: reflection.result event
        { status: IMO_COMPLIES/RETRIGGER_INVESTIGATION,
          resolved, network_status, gui_status, zscore_comparison,
          kpi_validation, gui_dashboard }

Usage:
  python server.py     ← dev (uvicorn reload, port 8082)
  python run.py        ← production (gunicorn uvicorn workers)
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
    check_execution_result,   # Tool 1: parse executor output → state["reflection_exec_result"]
    evaluate_and_publish,     # Tool 2: Spanner/MCP gates -> publish IMO_COMPLIES
)

from app.events import (
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
        "validates post-remediation state with Spanner/MCP gates, "
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

    exec_event:         execution.completed event dict (required)
                        Full event wrapper: { event_id, event_type, payload: {...} }
                        payload must contain: success, state, activation_id,
                        tmf641_order (with service_characteristics),
                        tmf921_intent (with expressions for KPI validation).

    pre_action_z_score: float (optional, recommended)
                        Pre-remediation z-score set by ReflexAgent call_gnn_engine.
                        Used for pre/post z-score comparison in evaluate_and_publish.
                        Falls back to original_gnn_event.anomalyScore.compositeScore,
                        then to 0.0 if neither is provided.

    original_gnn_event: dict (optional)
                        Original GNN event from ReflexAgent (gnn.anomaly.detected).
                        Used as fallback source for pre_action_z_score.
                        Mirrors reflection_service.py reference pattern.
    """
    exec_event:          Optional[Dict[str, Any]] = Field(None, description="execution.completed event")
    pre_action_z_score:  Optional[float]          = Field(None, description="Pre-action z-score from ReflexAgent (recommended)")
    original_gnn_event:  Optional[Dict[str, Any]] = Field(None, description="Original GNN event for pre-z fallback (optional)")

    class Config:
        extra = "allow"


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
      2. evaluate_and_publish    -> validates the three Spanner/MCP gates

    Resolution criteria (all three must be true):
      gate1: success=true AND state="completed"
      gate2: affected H3 anomaly_label rows are NORMAL
      gate3: affected entity performance rows are not degraded

    Returns reflection.result for downstream orchestration.

    Example request:
    {
      "exec_event": {
        "event_id": "evt-exec-001",
        "event_type": "execution.completed",
        "payload": {
          "success": true,
          "state": "completed",
          "activation_id": "ACT-SYN-002",
          "intent_id": "INT-001",
          "tmf921_intent": {
            "domain": "RAN",
            "root_cause": "antenna_tilt_misconfiguration",
            "expressions": [
              {"target_metric": "dl_throughput_mbps", "target_value": 50.0,
               "current_value": 8.0, "tolerance_pct": 10.0}
            ]
          },
          "tmf641_order": {"order_items": [{"service": {"service_characteristics":
            [{"name": "action_type", "value": "RAN_PARAM_ROLLBACK"}]}}]}
        }
      },
      "pre_action_z_score": 9.4,
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
    # All keys verified against validation_output dict in evaluate_and_publish:
    #   "status"           → "IMO_COMPLIES" or "RETRIGGER_INVESTIGATION"
    #   "resolved"         → bool
    #   "execution_ok"     → bool
    #   "zscore_comparison" -> {pre_action_z, baseline}
    #   "kpi_validation"   → [{metric, post_value, target, within_tolerance}]
    #   "gui_status"       → "HEALTHY_ENVIRONMENT" or "DEGRADED_ENVIRONMENT"
    #   "business_view"    → "UTILITY_SCORE_NORMAL" or "UTILITY_SCORE_DEGRADED"
    #   "service_view"     → "KEI_NORMAL" or "KEI_DEGRADED"
    #   "gnn_topology_view"→ "STABLE_ENVIRONMENT_GRAPH" or "UNSTABLE_ENVIRONMENT_GRAPH"
    #   "topology_state"   → "STABLE_GRAPH_V2" or "UNSTABLE"
    #   "retrigger_count"  → int
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
        "kpi_validation":    reflection_output.get("kpi_validation", []),
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
    port = int(os.environ.get("PORT", 8082))
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        log_level="info",
    )
