"""
Solution Planning Agent Tools — Stage 7
=========================================
Subscribes to:  investigation.rca.confirmed
Publishes:      solution.plan.ready

Reads confirmed RCA branches from event bus, generates TMF921 remediation intent.
"""

from google.adk.tools import ToolContext
import json

from app.events import (
    EVT_INVESTIGATION_RCA_CONFIRMED,
    consume_latest,
    publish_event,
    make_solution_plan_event,
)

_HEALING_ACTIONS = {
    "BAD_ANTENNA_TILT_PUSH": {
        "domain": "RAN",
        "ranked_healing_actions": [
            "ROLLBACK_TILT_TO_BASELINE",
            "REDUCE_TILT_BY_2_DEGREES",
            "REBUILD_NEIGHBOR_RELATIONS",
        ],
        "tmf915_parameter_bounds": {
            "parameter":    "antenna_tilt",
            "min_delta":    -2,
            "max_delta":    2,
            "safe_profile": "baseline_profile_v1",
        },
    },
    "HSS_STALE_SESSION_LOOP": {
        "domain": "CORE",
        "ranked_healing_actions": [
            "CLEAR_STALE_HSS_SESSIONS",
            "SHIFT_TRAFFIC_TO_SECONDARY_HSS",
            "REDUCE_REATTACH_RATE_LIMIT",
        ],
        "tmf915_parameter_bounds": {
            "parameter":    "stale_sessions",
            "max_clear":    10000,
            "safe_profile": "clear_looped_503_sessions",
        },
    },
    "PATH_DEGRADATION": {
        "domain": "TRANSPORT",
        "ranked_healing_actions": [
            "RESET_TRANSPORT_PATH",
            "FAILOVER_TO_BACKUP_PATH",
        ],
        "tmf915_parameter_bounds": {
            "parameter":    "transport_path",
            "safe_profile": "backup_path_v1",
        },
    },
    "MULTI_DOMAIN_SERVICE_DEGRADATION": {
        "domain": "CROSS_DOMAIN",
        "ranked_healing_actions": [
            "ROLLBACK_TILT_TO_BASELINE",
            "CLEAR_STALE_HSS_SESSIONS",
            "RESET_TRANSPORT_PATH",
        ],
        "tmf915_parameter_bounds": {
            "parameter":    "multi_domain",
            "safe_profile": "cross_domain_v1",
        },
    },
}


def generate_healing_plan(tool_context: ToolContext) -> dict:
    """
    Stage 7 — Solution Planning Agent tool.

    Subscribes to: investigation.rca.confirmed
    Publishes:     solution.plan.ready  (TMF921 remediation intent)
    """

    state = tool_context.state

    # Subscribe: read confirmed RCA event
    rca_event = consume_latest(state, EVT_INVESTIGATION_RCA_CONFIRMED)
    if not rca_event:
        return {
            "status": "IDLE",
            "reason": "No investigation.rca.confirmed event found"
        }

    # ── Idempotency (CRITICAL FIX) ───────────────────────────────────────
    last_processed = state.get("solution_last_event_id")

    if last_processed == rca_event["event_id"]:
        return {
            "status": "SKIPPED",
            "reason": "Event already processed",
            "event_id": last_processed
        }

    rca_output = rca_event["payload"]
    analysis   = rca_output["rootCauseAnalysis"]
    source_id = rca_event["event_id"]

    # Build healing branches from confirmedRcaBranches (multi-domain primary)
    rca_branches     = rca_output.get("confirmedRcaBranches") or []
    if not rca_branches:
        # fallback to single-cause RCA
        root_cause = analysis["confirmedRootCause"]

        rca_branches = [{
            "action_id": "A",
            "domain": analysis["domain"],
            "root_cause": root_cause,
            "priority_score": 10
        }]
    healing_branches = []

    for branch in rca_branches:
        root_cause  = branch["root_cause"]
        actions_def = _HEALING_ACTIONS.get(root_cause, {})
        if not actions_def:
            actions_def = {
                "ranked_healing_actions": ["MANUAL_INVESTIGATION_REQUIRED"]
            }

        entry = {
            "action_id":              branch["action_id"],
            "domain":                 branch["domain"],
            "root_cause":             root_cause,
            "priority_score":         branch["priority_score"],
            "ranked_healing_actions": actions_def.get("ranked_healing_actions", []),
        }

        if "tmf915_parameter_bounds" in actions_def:
            entry["tmf915_parameter_bounds"] = actions_def["tmf915_parameter_bounds"]

        healing_branches.append(entry)

    # Sort descending - highest business impact first (A before B before C)
    healing_branches.sort(key=lambda x: x["priority_score"], reverse=True)

    plan_output = {
        "event_id":           rca_output["eventId"],
        "source_event_id": source_id,
        "intent_type":        "TMF921_REMEDIATION_INTENT",
        "intent_target":      "ExecutionAgent",
        "priority":           analysis["severity"],
        "business_priority":  rca_output["businessPriority"],
        "affected_resources": analysis["affectedResources"],
        "healing_branches":   healing_branches,
        "execution_order":    [b["action_id"] for b in healing_branches],
    }

    # ── PRINT FULL OUTPUT (FOR DEBUG / DEMO) ─────────────────────────────
    print("\n================ SolutionPlanningAgent OUTPUT ================")
    print(json.dumps(plan_output, indent=2))
    print("==============================================================")

    # Publish: solution.plan.ready -> ExecutionAgent subscribes
    event = make_solution_plan_event(
        source_event_id = source_id,
        plan_output     = plan_output,
    )
    publish_event(state, event)

    # ── Persist state (CRITICAL for ValidationAgent) ─────────────────────
    state["solution_last_event_id"] = rca_event["event_id"]
    state["solution_output"] = plan_output

    return {
        "status":           "EVENT_PUBLISHED",
        "published_event":  event["event_type"],
        "event_id":         event["event_id"],
        "intent_type":      "TMF921_REMEDIATION_INTENT",
        "branch_count":     len(healing_branches),
        "execution_order":  plan_output["execution_order"],
        "next_agent":       "ExecutionAgent",
        "network_status":   "HEALING",
    }