"""
Execution Agent Mock API — Stage 8/9
====================================

Simulates Ericsson Execution Agent.

In production this will be replaced with:
- TMF641 API calls
- Ericsson Orchestrator / rApp
- Automation Engine

For now:
- Takes solution plan
- Returns execution result
"""

from execution_mock_output import generate_execution_output


def fetch_execution_result(plan_payload: dict) -> dict:
    """
    Simulate execution result based on plan.

    Future:
        - Send TMF921 intent to Execution Agent
        - Receive TMF641 sub-order results
    """

    branch_count = len(plan_payload.get("healing_branches", []))

    # Simple scenario logic (extend later)
    if branch_count >= 3:
        scenario = "UC1_SUCCESSFUL_REMEDIATION"
    else:
        scenario = "UC1_SUCCESSFUL_REMEDIATION"

    return generate_execution_output(scenario=scenario)


def publish_execution_event(payload: dict) -> dict:
    """
    Mock publish layer (for symmetry)

    Real system:
        - publish_event handles event bus
        - this becomes external API call
    """
    return {
        "status": "published",
        "target_agent": "ValidationAgent",
        "payload": payload,
    }