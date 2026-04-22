"""
execution_agent/mock_api.py
=============================
Stage 9 fetch (execution result from Ericsson) and Stage 9→10 publish.

FETCH: get TMF641 execution result (mock → real Ericsson Automation Engine)
PUBLISH: hand off result to ValidationAgent (mock → real A2A call)
"""

from execution_mock_output import generate_execution_output
from app.config.remediation_config import get_execution_scenario


def fetch_execution_result(plan_payload: dict) -> dict:
    """
    Stage 9 — Fetch execution result from Ericsson ExecutionAgent.

    Derives scenario from actual domains in the plan — no hardcoding.
    Production replacement: GET TMF641 sub-order status from Ericsson Automation Engine.
    """
    domains = {
        b["domain"]
        for b in plan_payload.get("healing_branches", [])
    }
    scenario = get_execution_scenario(domains)
    print(f"[ExecutionAgent mock_api] Execution scenario: {scenario} (domains={domains})")
    return generate_execution_output(scenario=scenario)


def publish_execution_event(execution_payload: dict) -> dict:
    """
    Stage 9 → Stage 10.
    Publishes execution result to ValidationAgent.

    Production replacement: POST to ValidationAgent A2A endpoint.
    """
    return {
        "status":           "published",
        "target_agent":     "ValidationAgent",
        "event_type":       "execution.remediation.completed",
        "execution_status": execution_payload.get("executionStatus"),
        "branch_results": {
            b["domain"]: b["status"]
            for b in execution_payload.get("executionBranches", [])
        },
    }