"""
app/agents/solution_planning_agent/tools.py
=============================================
Stage 7 — Solution Planning Agent.

Flow:
  1. Read confirmed RCA branches from event bus
  2. For each branch: look up healing actions from remediation_config
  3. Compute exact parameter corrections (tilt degrees, session counts)
     using get_tilt_correction() / bounds from remediation_config
  4. Build TMF921 remediation intent with action_command per branch
  5. PUBLISH via mock_api.publish_solution_plan() (→ ExecutionAgent)
  6. Write solution.plan.ready to session state event bus

All domain knowledge, parameter bounds, action names → remediation_config.py
API publish call                                     → mock_api.py
"""

import json

from google.adk.tools import ToolContext

from app.events import (
    EVT_INVESTIGATION_RCA_CONFIRMED,
    consume_latest,
    make_solution_plan_event,
    publish_event,
)
from app.config.remediation_config import (
    get_healing_actions,
    get_tilt_correction,
    HEALING_ACTIONS,
)
from app.agents.solution_planning_agent.mock_api import publish_solution_plan


# ── Per-branch action command builder ─────────────────────────────────────────

def _build_action_command(root_cause: str, rca_output: dict, actions_def: dict) -> dict:
    """
    Build the exact action_command for a branch.
    This is what ExecutionAgent reads to create Netconf/YANG payloads.

    For antenna tilt:  derives exact degrees from RCA rcaDetails
    For HSS sessions:  derives exact session count from RCA rcaDetails
    For transport:     selects backup path from config
    For unknown:       returns MANUAL_INVESTIGATION_REQUIRED
    """
    rca_details = rca_output.get("rcaDetails", {})
    bounds      = actions_def.get("tmf915_parameter_bounds", {})

    if root_cause == "BAD_ANTENNA_TILT_PUSH":
        current_tilt  = rca_details.get("observed_tilt_degrees")
        baseline_tilt = rca_details.get("baseline_tilt_degrees")

        if current_tilt is not None:
            correction = get_tilt_correction(
                current_tilt  = current_tilt,
                baseline_tilt = baseline_tilt,   # None → uses config default (3.0°)
            )
            return {
                "type":               "ANTENNA_TILT_ADJUST",
                "current_degrees":    correction["current_tilt_degrees"],
                "target_degrees":     correction["target_tilt_degrees"],
                "delta_degrees":      correction["correction_delta"],
                "within_safe_bounds": correction["within_safe_bounds"],
                "clamped":            correction["clamped"],
                "description": (
                    f"Rollback antenna tilt: {current_tilt}° → "
                    f"{correction['target_tilt_degrees']}° "
                    f"({correction['correction_delta']:+.2f}°)"
                ),
            }
        else:
            # GNN / investigation didn't carry observed tilt — use safe default
            baseline = bounds.get("baseline_value", 3.0)
            return {
                "type":        "ANTENNA_TILT_ROLLBACK_DEFAULT",
                "target_degrees": baseline,
                "delta_degrees":  None,
                "description": f"Set antenna tilt to nominal {baseline}° (observed not available)",
            }

    if root_cause in ("HSS_STALE_SESSION_LOOP", "HSS_SATURATION"):
        observed  = rca_details.get("observed_session_count")
        max_cap   = rca_details.get("hss_max_capacity")
        max_clear = bounds.get("max_clear", 10000)
        target_pct = bounds.get("target_capacity_pct", 80)

        if observed and max_cap:
            target_count = int(max_cap * target_pct / 100)
            to_clear     = max(0, min(observed - target_count, max_clear))
            desc = (
                f"Clear {to_clear:,} stale HSS sessions "
                f"({observed:,} observed → target {target_count:,} = {target_pct}% capacity)"
            )
        else:
            to_clear = max_clear
            desc = f"Clear up to {max_clear:,} stale HSS sessions (exact count not available)"

        return {
            "type":                "HSS_SESSION_CLEAR",
            "sessions_to_clear":   to_clear,
            "target_capacity_pct": target_pct,
            "description":         desc,
        }

    if root_cause in ("FIBER_CUT", "PATH_DEGRADATION"):
        return {
            "type":         "TRANSPORT_FAILOVER",
            "backup_path":  bounds.get("backup_path", "AGG_REDUNDANT"),
            "safe_profile": bounds.get("safe_profile", "backup_path_v1"),
            "description":  "Failover backhaul to redundant path",
        }

    if root_cause == "MULTI_DOMAIN_SERVICE_DEGRADATION":
        return {
            "type":        "MULTI_DOMAIN_SEQUENCE",
            "safe_profile": bounds.get("safe_profile", "cross_domain_v1"),
            "description": "Execute multi-domain remediation sequence",
        }

    return {
        "type":        "MANUAL_INVESTIGATION_REQUIRED",
        "description": f"Unknown root cause: {root_cause}",
    }


def _build_healing_branch(branch: dict, rca_output: dict) -> dict:
    """Build one complete healing branch entry."""
    root_cause  = branch["root_cause"]
    actions_def = get_healing_actions(root_cause)       # ← from remediation_config
    bounds      = dict(actions_def.get("tmf915_parameter_bounds", {}))
    action_cmd  = _build_action_command(root_cause, rca_output, actions_def)

    # Annotate bounds with the runtime values used in action_cmd
    if action_cmd.get("current_degrees") is not None:
        bounds["current_value"] = action_cmd["current_degrees"]
    if action_cmd.get("target_degrees") is not None:
        bounds["target_value"] = action_cmd["target_degrees"]

    return {
        "action_id":               branch["action_id"],
        "domain":                  branch.get("domain", actions_def["domain"]),
        "root_cause":              root_cause,
        "priority_score":          branch.get("priority_score", 0),
        "ranked_healing_actions":  actions_def["ranked_healing_actions"],
        "expected_recovery_min":   actions_def["expected_recovery_minutes"],
        "synth_signals_observed":  actions_def.get("synth_signal", []),
        "action_command":          action_cmd,           # ← exact ExecutionAgent instruction
        "tmf915_parameter_bounds": bounds,
    }


# ── Main tool ──────────────────────────────────────────────────────────────────

def generate_healing_plan(tool_context: ToolContext) -> dict:
    """
    Stage 7 — Solution Planning Agent tool.

    Subscribes to: investigation.rca.confirmed
    Publishes:     solution.plan.ready (TMF921)
    """

    state = tool_context.state

    # ── Subscribe ──────────────────────────────────────────────────────────
    rca_event = consume_latest(state, EVT_INVESTIGATION_RCA_CONFIRMED)
    if not rca_event:
        return {
            "status": "IDLE",
            "reason": "No investigation.rca.confirmed event found",
        }

    # ── Idempotency ────────────────────────────────────────────────────────
    if state.get("solution_last_event_id") == rca_event["event_id"]:
        return {
            "status":   "SKIPPED",
            "reason":   "Event already processed",
            "event_id": rca_event["event_id"],
        }

    rca_output = rca_event["payload"]
    analysis   = rca_output["rootCauseAnalysis"]
    source_id  = rca_event["event_id"]

    # ── Build branch list ──────────────────────────────────────────────────
    rca_branches = rca_output.get("confirmedRcaBranches") or [{
        "action_id":      "A",
        "domain":         analysis["domain"],
        "root_cause":     analysis["confirmedRootCause"],
        "priority_score": 10,
    }]

    # ── Build healing branches with exact action commands ──────────────────
    healing_branches = [
        _build_healing_branch(branch, rca_output)
        for branch in rca_branches
    ]
    healing_branches.sort(key=lambda b: b["priority_score"], reverse=True)

    # ── Compose TMF921 intent ──────────────────────────────────────────────
    plan_output = {
        "event_id":           rca_output["eventId"],
        "source_event_id":    source_id,
        "intent_type":        "TMF921_REMEDIATION_INTENT",
        "intent_target":      "ExecutionAgent",
        "priority":           analysis["severity"],
        "business_priority":  rca_output["businessPriority"],
        "affected_resources": analysis["affectedResources"],
        "healing_branches":   healing_branches,
        "execution_order":    [b["action_id"] for b in healing_branches],
    }

    print("\n================ SolutionPlanningAgent — Stage 7 ================")
    print(json.dumps(plan_output, indent=2, default=str))
    print("==================================================================")

    # ── Publish downstream via mock_api ────────────────────────────────────
    handoff_receipt = publish_solution_plan(plan_output)        # ← mock_api call
    print(f"[SolutionPlanningAgent] Downstream handoff: {handoff_receipt['target_agent']}")

    # ── Write event to session state ───────────────────────────────────────
    event = make_solution_plan_event(source_event_id=source_id, plan_output=plan_output)
    publish_event(state, event)

    state["solution_last_event_id"] = rca_event["event_id"]
    state["solution_output"]        = plan_output

    # Human-readable action summary for return value
    action_summary = [
        {
            "branch":  b["action_id"],
            "domain":  b["domain"],
            "action":  b["action_command"].get("description", "N/A"),
        }
        for b in healing_branches
    ]

    return {
        "status":           "EVENT_PUBLISHED",
        "published_event":  event["event_type"],
        "event_id":         event["event_id"],
        "intent_type":      "TMF921_REMEDIATION_INTENT",
        "branch_count":     len(healing_branches),
        "execution_order":  plan_output["execution_order"],
        "action_summary":   action_summary,
        "handoff_receipt":  handoff_receipt,
        "next_agent":       "ExecutionAgent",
        "network_status":   "HEALING",
    }
