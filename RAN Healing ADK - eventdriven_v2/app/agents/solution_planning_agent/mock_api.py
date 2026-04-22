"""
app/agents/solution_planning_agent/mock_api.py
================================================
Stage 7 → Stage 9 PUBLISH.

PUBLISH: push TMF921 remediation intent to ExecutionAgent.
         mock  → receipt dict (ExecutionAgent reads from session state)
         prod  → POST to Ericsson TMF641 Execution A2A endpoint

No FETCH here — SolutionPlanning reads from the session state event bus
(investigation.rca.confirmed). Investigation PUSHES to Solution; Solution
does NOT call Investigation.
"""

from app.config.remediation_config import get_execution_scenario


def publish_solution_plan(plan_output: dict) -> dict:
    """
    Stage 7 → Stage 9.
    Publish TMF921 remediation intent to ExecutionAgent.

    Derives execution_scenario from the domains present in the plan so
    ExecutionAgent mock knows which scenario result to produce.

    Production replacement:
        POST https://ericsson-execution-agent/v1/execute
        body: plan_output
    """
    domains = {
        b["domain"]
        for b in plan_output.get("healing_branches", [])
    }
    execution_scenario = get_execution_scenario(domains)

    return {
        "status":              "published",
        "target_agent":        "ExecutionAgent",
        "event_type":          "solution.plan.ready",
        "tmf_event_type":      "tmf921.remediation.intent",
        "execution_scenario":  execution_scenario,
        "branch_count":        len(plan_output.get("healing_branches", [])),
        "execution_order":     plan_output.get("execution_order", []),
    }
