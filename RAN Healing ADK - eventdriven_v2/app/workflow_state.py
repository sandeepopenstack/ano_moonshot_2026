"""
workflow_state.py — Session State Schema & Summary Helper
===========================================================
Documents what lives in tool_context.state at each pipeline stage.
Used for IDE support, Cloud Logging (Stage 11), and audit.

State structure (event-driven):
  state["network_status"]              str       HEALTHY | ANOMALY_DETECTED | HEALING | RESOLVED
  state["event_bus"]                   list      ordered log of all published events
  state["latest_gnn_anomaly_detected"] dict      latest GNN event
  state["latest_monitoring_triage_ready"] dict   latest triage event
  state["latest_investigation_rca_confirmed"] dict
  state["latest_solution_plan_ready"]  dict
  state["latest_execution_completed"]  dict
  state["latest_validation_result"]    dict
"""

from __future__ import annotations
from typing import Any, Optional

from app.events import (
    EVT_GNN_ANOMALY_DETECTED,
    EVT_MONITORING_TRIAGE_READY,
    EVT_INVESTIGATION_RCA_CONFIRMED,
    EVT_SOLUTION_PLAN_READY,
    EVT_EXECUTION_COMPLETED,
    EVT_VALIDATION_RESULT,
    NETWORK_STATUS_KEY,
    EVENT_BUS_KEY,
    consume_latest,
)


def extract_final_summary(state: dict[str, Any]) -> dict[str, Any]:
    """
    Extract concise pipeline summary from final session state.
    Called in main.py after Runner completes — feeds Cloud Logging (Stage 11).
    """
    monitoring  = (consume_latest(state, EVT_MONITORING_TRIAGE_READY)  or {}).get("payload", {})
    rca         = (consume_latest(state, EVT_INVESTIGATION_RCA_CONFIRMED) or {}).get("payload", {})
    plan        = (consume_latest(state, EVT_SOLUTION_PLAN_READY)       or {}).get("payload", {})
    execution   = (consume_latest(state, EVT_EXECUTION_COMPLETED)       or {}).get("payload", {})
    validation  = (consume_latest(state, EVT_VALIDATION_RESULT)         or {}).get("payload", {})

    event_bus   = state.get(EVENT_BUS_KEY, [])

    return {
        "pipeline":         "RAN_SELF_HEALING",
        "network_status":   state.get(NETWORK_STATUS_KEY),
        "event_count":      len(event_bus),
        "event_sequence":   [e["event_type"] for e in event_bus],

        "stage_5_monitoring": {
            "domain_triage":     monitoring.get("domain_triage"),
            "priority_flag":     monitoring.get("priority_flag"),
            "business_priority": monitoring.get("business_priority"),
            "execution_order":   monitoring.get("execution_order"),
        },

        "stage_6_investigation": {
            "root_cause": (rca.get("rootCauseAnalysis") or {}).get("confirmedRootCause"),
            "domain":     (rca.get("rootCauseAnalysis") or {}).get("domain"),
            "severity":   (rca.get("rootCauseAnalysis") or {}).get("severity"),
            "rca_branches": len(rca.get("confirmedRcaBranches") or []),
        },

        "stage_7_solution": {
            "intent_type":     plan.get("intent_type"),
            "execution_order": plan.get("execution_order"),
            "branch_count":    len(plan.get("healing_branches") or []),
        },

        "stage_9_execution": {
            "execution_status": execution.get("executionStatus"),
            "branch_results": {
                b["domain"]: b["status"]
                for b in (execution.get("executionBranches") or [])
            },
        },

        "stage_10_validation": {
            "status":            validation.get("status"),
            "post_action_score": validation.get("post_action_score"),
            "expected_baseline": validation.get("expected_baseline"),
            "gui_status":        validation.get("gui_status"),
            "gnn_topology_view": validation.get("gnn_topology_view"),
            "business_view":     validation.get("business_view"),
            "service_view":      validation.get("service_view"),
        },

        "resolved": state.get(NETWORK_STATUS_KEY) == "RESOLVED",
    }
