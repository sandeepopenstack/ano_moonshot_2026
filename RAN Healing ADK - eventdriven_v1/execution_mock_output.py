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
) -> dict:
    """
    Stage 9 → Stage 10 mock producer.

    Scenarios
    ---------
    UC1_SUCCESSFUL_REMEDIATION : all branches succeed, z-score returns to baseline
    default (PARTIAL_SUCCESS)  : transport branch fails, z-score still elevated
    """
    event_id = str(uuid.uuid4())

    if scenario == "UC1_SUCCESSFUL_REMEDIATION":
        return {
            "eventId":   event_id,
            "eventType": "execution.remediation.completed",
            "eventTime": datetime.now(timezone.utc).isoformat(),

            # Per-domain branch execution results (TMF641 Sub-Orders)
            "executionBranches": [
                {"action_id": "A", "domain": "RAN",       "status": "SUCCESS"},
                {"action_id": "B", "domain": "CORE",      "status": "SUCCESS"},
                {"action_id": "C", "domain": "TRANSPORT", "status": "SUCCESS"},
            ],

            # Post-action GNN re-inference snapshot (PPT Stage 10)
            "postActionValidation": {
                "postActionZScore":  1.2,
                "expectedBaseline":  2.0,
                "topologySnapshot":  "STABLE_GRAPH_V2",
                "serviceKPI":        "KEI_NORMAL",       # GUI: Service view
                "businessUtility":   "UTILITY_SCORE_NORMAL",  # GUI: Business view
            },
            "executionStatus": "SUCCESS",
        }
    
    if scenario == "UC2_CORE_REMEDIATION":
        return {
            "eventId":   event_id,
            "eventType": "execution.remediation.completed",
            "eventTime": datetime.now(timezone.utc).isoformat(),
            "executionBranches": [
                {"action_id": "A", "domain": "CORE", "status": "SUCCESS"},
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

    # Partial success — transport branch failed
    return {
        "eventId":   event_id,
        "eventType": "execution.remediation.completed",
        "eventTime": datetime.now(timezone.utc).isoformat(),
        "executionBranches": [
            {"action_id": "A", "domain": "RAN",       "status": "SUCCESS"},
            {"action_id": "B", "domain": "CORE",      "status": "SUCCESS"},
            {"action_id": "C", "domain": "TRANSPORT", "status": "FAILED"},
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