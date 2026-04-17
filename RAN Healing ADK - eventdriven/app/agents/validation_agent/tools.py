"""
Validation Agent — Tools
=========================
PPT Stage 10: validate post-remediation recovery.

KEY FIXES vs original:
  • Removed broken imports (matplotlib.style, unittest.result — not needed here).
  • Uses `tool_context: ToolContext` (correct ADK signature).
  • Reads solution_output from session state for audit cross-check.
  • Writes validation_output to session state (audit trail + Cloud Logging Stage 11).
  • Returns both IMO_COMPLIES and RETRIGGER_INVESTIGATION paths as per PPT.
"""

from google.adk.tools import ToolContext
import json

from app.events import (
    EVT_EXECUTION_COMPLETED,
    consume_latest,
    publish_event,
    make_validation_result_event,
)


def validate_remediation(tool_context: ToolContext) -> dict:
    """
    Stage 10 — Validation Agent tool.

    Subscribes to: execution.completed
    Publishes:     validation.result
    """

    state = tool_context.state

    # ── Subscribe ───────────────────────────────────────────────────────
    exec_event = consume_latest(state, EVT_EXECUTION_COMPLETED)

    if not exec_event:
        return {
            "status": "IDLE",
            "reason": "No execution.completed event found"
        }

    # ── Idempotency ─────────────────────────────────────────────────────
    last_processed = state.get("validation_last_event_id")

    if last_processed == exec_event["event_id"]:
        return {
            "status": "SKIPPED",
            "reason": "Event already processed",
            "event_id": last_processed
        }

    result = exec_event["payload"]
    source_id = exec_event["event_id"]

    validation = result["postActionValidation"]
    branches   = result["executionBranches"]

    post_z   = validation["postActionZScore"]
    baseline = validation["expectedBaseline"]

    all_ok   = all(b["status"] == "SUCCESS" for b in branches)
    resolved = (post_z <= baseline) and all_ok

    failed = [b for b in branches if b["status"] != "SUCCESS"]

    validation_output = {
        "event_id":          result["eventId"],
        "source_event_id":   source_id,
        "post_action_score": post_z,
        "expected_baseline": baseline,
        "topology_state":    validation["topologySnapshot"],
        "business_view":     validation["businessUtility"],
        "service_view":      validation["serviceKPI"],
        "branches_status":   branches,
        "failed_branches":   failed,
        "status":            "IMO_COMPLIES" if resolved else "RETRIGGER_INVESTIGATION",
        "gui_status":        "HEALTHY_ENVIRONMENT" if resolved else "DEGRADED_ENVIRONMENT",
        "gnn_topology_view": "STABLE_ENVIRONMENT_GRAPH" if resolved else "UNSTABLE_ENVIRONMENT_GRAPH",
    }

    if not resolved:
        validation_output["next_target"] = "InvestigationAgent"

    # ── Publish event ───────────────────────────────────────────────────
    event = make_validation_result_event(
        source_event_id   = source_id,
        resolved          = resolved,
        validation_output = validation_output,
    )

    publish_event(state, event)

    # ── LOOP TRIGGER  ──────────────────────────────────────
    from app.events import make_gnn_anomaly_event

    if not resolved:
        new_gnn_event = make_gnn_anomaly_event({
            "retriggered": True,
            "previous_failure": validation_output
        })

        publish_event(state, new_gnn_event)


    # ── Persist state ───────────────────────────────────────────────────
    state["validation_last_event_id"] = exec_event["event_id"]
    state["validation_output"] = validation_output

    # ── PRINT FULL JSON OUTPUT ───────────────────────────────────
    print("\n================ VALIDATION AGENT OUTPUT ================")
    print(json.dumps(validation_output, indent=2, default=str))
    print("=========================================================\n")

    return {
        "status":            "EVENT_PUBLISHED",
        "published_event":   event["event_type"],
        "event_id":          event["event_id"],
        "resolved":          resolved,
        "post_action_score": post_z,
        "gui_status":        validation_output["gui_status"],
        "gnn_topology_view": validation_output["gnn_topology_view"],
        "business_view":     validation_output["business_view"],
        "service_view":      validation_output["service_view"],
        "network_status":    "RESOLVED" if resolved else "ANOMALY_DETECTED",
    }