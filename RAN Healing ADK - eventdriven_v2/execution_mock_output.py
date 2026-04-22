"""
Execution Agent Mock Output — Stage 9 (PPT)
============================================
The Execution Agent (Ericsson team) sends TMF641 Sub-Orders to the
Automation Engine (Stage 8), which applies changes to RAN / CORE / TRANSPORT.
This module mocks the post-execution result consumed by ValidationAgent (Stage 10).

PPT Stage 9 contract:
  In:  TMF921 remediation intent (from SolutionPlanningAgent)
  Out: TMF641 Sub-Order execution result per domain branch
       + post-action validation snapshot (z-score, topology, KPI)
"""

from datetime import datetime, timezone
import uuid


def generate_execution_output(
    scenario: str = "UC1_SUCCESSFUL_REMEDIATION",
    healing_branches: list | None = None,
) -> dict:
    """
    Stage 9 → Stage 10 mock producer.

    Scenarios
    ---------
    UC1_SUCCESSFUL_REMEDIATION  : all branches succeed, z-score returns to baseline
    UC2_CORE_REMEDIATION        : HSS session clear succeeds
    UC3_TRANSPORT_REMEDIATION   : fiber failover succeeds
    default (PARTIAL_SUCCESS)   : transport branch fails, z-score still elevated

    healing_branches: if supplied, each branch result echoes back the
    action_command from the plan — gives ValidationAgent a full audit trail.
    """
    event_id = str(uuid.uuid4())

    def _branch(action_id: str, domain: str, status: str) -> dict:
        result = {"action_id": action_id, "domain": domain, "status": status}
        if healing_branches:
            for b in healing_branches:
                if b.get("action_id") == action_id:
                    cmd = b.get("action_command")
                    if cmd:
                        result["executed_action"] = cmd
                    break
        return result

    if scenario == "UC1_SUCCESSFUL_REMEDIATION":
        return {
            "eventId":   event_id,
            "eventType": "execution.remediation.completed",
            "eventTime": datetime.now(timezone.utc).isoformat(),
            "executionBranches": [
                _branch("A", "RAN",       "SUCCESS"),
                _branch("B", "CORE",      "SUCCESS"),
                _branch("C", "TRANSPORT", "SUCCESS"),
            ],
            "postActionValidation": {
                "postActionZScore":  1.2,
                "expectedBaseline":  2.0,
                "topologySnapshot":  "STABLE_GRAPH_V2",
                "serviceKPI":        "KEI_NORMAL",
                "businessUtility":   "UTILITY_SCORE_NORMAL",
            },
            "executionStatus": "SUCCESS",
        }

    if scenario == "UC2_CORE_REMEDIATION":
        return {
            "eventId":   event_id,
            "eventType": "execution.remediation.completed",
            "eventTime": datetime.now(timezone.utc).isoformat(),
            "executionBranches": [
                _branch("A", "CORE", "SUCCESS"),
            ],
            "postActionValidation": {
                "postActionZScore":  1.4,
                "expectedBaseline":  2.0,
                "topologySnapshot":  "STABLE_GRAPH_CORE_V2",
                "serviceKPI":        "KEI_NORMAL",
                "businessUtility":   "UTILITY_SCORE_NORMAL",
            },
            "executionStatus": "SUCCESS",
        }

    if scenario == "UC3_TRANSPORT_REMEDIATION":
        return {
            "eventId":   event_id,
            "eventType": "execution.remediation.completed",
            "eventTime": datetime.now(timezone.utc).isoformat(),
            "executionBranches": [
                _branch("A", "TRANSPORT", "SUCCESS"),
            ],
            "postActionValidation": {
                "postActionZScore":  1.6,
                "expectedBaseline":  2.0,
                "topologySnapshot":  "STABLE_GRAPH_TRANSPORT_V2",
                "serviceKPI":        "KEI_NORMAL",
                "businessUtility":   "UTILITY_SCORE_NORMAL",
            },
            "executionStatus": "SUCCESS",
        }

    # Partial success — transport branch failed → triggers ValidationAgent retrigger
    return {
        "eventId":   event_id,
        "eventType": "execution.remediation.completed",
        "eventTime": datetime.now(timezone.utc).isoformat(),
        "executionBranches": [
            _branch("A", "RAN",       "SUCCESS"),
            _branch("B", "CORE",      "SUCCESS"),
            _branch("C", "TRANSPORT", "FAILED"),
        ],
        "postActionValidation": {
            "postActionZScore":  4.8,
            "expectedBaseline":  2.0,
            "topologySnapshot":  "UNSTABLE_GRAPH_V2",
            "serviceKPI":        "KEI_DEGRADED",
            "businessUtility":   "UTILITY_SCORE_DEGRADED",
        },
        "executionStatus": "PARTIAL_SUCCESS",
    }


def publish_execution_output(payload: dict) -> dict:
    """Stage 9 → Stage 10 handoff to ValidationAgent."""
    return {
        "status":       "published",
        "target_agent": "ValidationAgent",
        "eventType":    "execution.remediation.completed",
        "payload":      payload,
    }