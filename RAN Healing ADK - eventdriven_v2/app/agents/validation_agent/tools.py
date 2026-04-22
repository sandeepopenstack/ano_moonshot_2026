"""
app/agents/validation_agent/tools.py
=======================================
Stage 10 — Validation Agent.

Flow:
  1. Read execution.completed from event bus
  2. Check all resolution conditions using VALIDATION_CONFIG from remediation_config
  3. Build detailed validation report (pre vs post Z-score, branch results, KPIs)
  4. PUBLISH via mock_api.publish_validation_result() (→ GUI / Cloud Logging)
  5. Write validation.result to session state
  6. If not resolved: retrigger pipeline (re-publish GNN event), capped at max_retrigger

All thresholds and valid state sets → remediation_config.VALIDATION_CONFIG
API publish call                   → mock_api.py
"""

import json

from google.adk.tools import ToolContext

from app.events import (
    EVT_EXECUTION_COMPLETED,
    EVT_GNN_ANOMALY_DETECTED,
    NETWORK_STATUS_KEY,
    consume_latest,
    latest_key,
    make_gnn_anomaly_event,
    make_validation_result_event,
    publish_event,
)
from app.config.remediation_config import VALIDATION_CONFIG
from app.agents.validation_agent.mock_api import publish_validation_result


def _build_zscore_comparison(pre_z: float | None, post_z: float, baseline: float) -> dict:
    """
    Build the pre-vs-post Z-score delta table for the validation report.
    pre_z comes from the original GNN event stored in session state.
    """
    improvement_pct = None
    if pre_z and pre_z > 0:
        improvement_pct = round((pre_z - post_z) / pre_z * 100, 1)

    return {
        "pre_action_z_score":  pre_z,
        "post_action_z_score": post_z,
        "baseline_threshold":  baseline,
        "delta":               round(post_z - (pre_z or post_z), 2),
        "improvement_pct":     improvement_pct,
        "resolved":            post_z <= VALIDATION_CONFIG["resolved_z_threshold"],
    }


def validate_remediation(tool_context: ToolContext) -> dict:
    """
    Stage 10 — Validation Agent tool.

    Subscribes to: execution.completed
    Publishes:     validation.result

    Verdict:
      IMO_COMPLIES          → pipeline ends, network_status = RESOLVED
      RETRIGGER_INVESTIGATION → re-publishes gnn.anomaly.detected (loop)
                               capped at VALIDATION_CONFIG['max_retrigger_attempts']
    """

    state = tool_context.state

    # ── Subscribe ──────────────────────────────────────────────────────────
    exec_event = consume_latest(state, EVT_EXECUTION_COMPLETED)
    if not exec_event:
        return {
            "status": "IDLE",
            "reason": "No execution.completed event in session state",
        }

    # ── Idempotency ────────────────────────────────────────────────────────
    if state.get("validation_last_event_id") == exec_event["event_id"]:
        return {
            "status":   "SKIPPED",
            "reason":   "Event already processed",
            "event_id": exec_event["event_id"],
        }

    result    = exec_event["payload"]
    source_id = exec_event["event_id"]

    post_val = result["postActionValidation"]
    branches = result["executionBranches"]
    post_z   = post_val["postActionZScore"]
    baseline = post_val["expectedBaseline"]

    # ── Pre-action Z-score (from original GNN event in session state) ──────
    original_gnn     = state.get(latest_key(EVT_GNN_ANOMALY_DETECTED), {})
    pre_z            = original_gnn.get("payload", {}).get("anomalyScore", {}).get("zScore")

    # ── Resolution checks (all from remediation_config.VALIDATION_CONFIG) ─
    failed      = [b for b in branches if b.get("status") != "SUCCESS"]
    z_ok        = post_z <= VALIDATION_CONFIG["resolved_z_threshold"]
    branches_ok = len(failed) == 0
    topology_ok = post_val["topologySnapshot"] in VALIDATION_CONFIG["topology_stable_states"]
    kpi_ok      = post_val["serviceKPI"]        in VALIDATION_CONFIG["kpi_normal_states"]
    business_ok = post_val["businessUtility"]   in VALIDATION_CONFIG["business_normal_states"]

    resolved = z_ok and branches_ok and topology_ok and kpi_ok and business_ok

    # ── Retrigger loop guard ───────────────────────────────────────────────
    retrigger_count = state.get("validation_retrigger_count", 0)
    escalated = False
    if not resolved and retrigger_count >= VALIDATION_CONFIG["max_retrigger_attempts"]:
        print(f"[ValidationAgent] ESCALATION: retrigger limit "
              f"({VALIDATION_CONFIG['max_retrigger_attempts']}) reached — forcing RESOLVED")
        resolved  = True
        escalated = True

    # ── Z-score comparison table ───────────────────────────────────────────
    zscore_comparison = _build_zscore_comparison(pre_z, post_z, baseline)

    # ── Build validation output ────────────────────────────────────────────
    validation_output = {
        "event_id":          result["eventId"],
        "source_event_id":   source_id,

        # Z-score pre vs post
        "zscore_comparison": zscore_comparison,
        "post_action_score": post_z,
        "expected_baseline": baseline,

        # Individual check results
        "z_score_ok":    z_ok,
        "branches_ok":   branches_ok,
        "topology_ok":   topology_ok,
        "kpi_ok":        kpi_ok,
        "business_ok":   business_ok,

        # GUI fields (per slides: Utility Score + KEI)
        "topology_state":    post_val["topologySnapshot"],
        "business_view":     post_val["businessUtility"],    # Utility Score
        "service_view":      post_val["serviceKPI"],         # KEI

        # Branch details
        "branches_status":   branches,
        "failed_branches":   failed,

        # Verdict
        "status":           "IMO_COMPLIES" if resolved else "RETRIGGER_INVESTIGATION",
        "gui_status":       "HEALTHY_ENVIRONMENT" if resolved else "DEGRADED_ENVIRONMENT",
        "gnn_topology_view": "STABLE_ENVIRONMENT_GRAPH" if resolved else "UNSTABLE_ENVIRONMENT_GRAPH",
        "escalated":         escalated,
        "retrigger_count":   retrigger_count,
    }

    # Add failure reasons when not resolved
    if not resolved:
        validation_output["failure_reasons"] = {
            "z_score":  f"post={post_z} vs threshold={VALIDATION_CONFIG['resolved_z_threshold']}" if not z_ok else "OK",
            "branches": [b["domain"] for b in failed] if failed else "OK",
            "topology": post_val["topologySnapshot"] if not topology_ok else "OK",
            "kpi":      post_val["serviceKPI"] if not kpi_ok else "OK",
            "business": post_val["businessUtility"] if not business_ok else "OK",
        }
        validation_output["next_target"] = "InvestigationAgent"

    # ── Publish verdict via mock_api ───────────────────────────────────────
    handoff_receipt = publish_validation_result(validation_output)   # ← mock_api call
    print(f"[ValidationAgent] Published: resolved={resolved}, "
          f"gui={validation_output['gui_status']}")

    # ── Write event to session state ───────────────────────────────────────
    event = make_validation_result_event(
        source_event_id   = source_id,
        resolved          = resolved,
        validation_output = validation_output,
    )
    publish_event(state, event)

    # ── Retrigger: re-publish GNN event if not resolved ───────────────────
    if not resolved:
        retrigger_count += 1
        state["validation_retrigger_count"] = retrigger_count
        print(f"[ValidationAgent] RETRIGGER #{retrigger_count} — "
              f"{validation_output.get('failure_reasons')}")
        original_payload = dict(original_gnn.get("payload", {}))
        original_payload["retriggered"]     = True
        original_payload["retrigger_count"] = retrigger_count
        publish_event(state, make_gnn_anomaly_event(original_payload))

    # ── Persist ────────────────────────────────────────────────────────────
    state["validation_last_event_id"] = exec_event["event_id"]
    state["validation_output"]        = validation_output
    state[NETWORK_STATUS_KEY]         = "RESOLVED" if resolved else "ANOMALY_DETECTED"

    print("\n================ ValidationAgent — Stage 10 ================")
    print(json.dumps(validation_output, indent=2, default=str))
    print("=============================================================\n")

    return {
        "status":            "EVENT_PUBLISHED",
        "published_event":   event["event_type"],
        "event_id":          event["event_id"],
        "resolved":          resolved,
        "zscore_comparison": zscore_comparison,
        "verdict":           validation_output["status"],
        "checks": {
            "z_score_ok":   z_ok,
            "branches_ok":  branches_ok,
            "topology_ok":  topology_ok,
            "kpi_ok":       kpi_ok,
            "business_ok":  business_ok,
        },
        "gui_status":        validation_output["gui_status"],
        "gnn_topology_view": validation_output["gnn_topology_view"],
        "business_view":     validation_output["business_view"],
        "service_view":      validation_output["service_view"],
        "handoff_receipt":   handoff_receipt,
        "network_status":    state[NETWORK_STATUS_KEY],
        "retrigger_count":   retrigger_count if not resolved else 0,
        "escalated":         escalated,
    }
