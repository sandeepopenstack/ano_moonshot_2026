"""
Investigation Agent Mock API — Stage 6
======================================

This simulates Ericsson Investigation Agent behavior.

In production this will be replaced with:
- TMF Agent A2A API call
- Ericsson IMF / rApp endpoint
- Possibly Pub/Sub or REST endpoint

For now:
- Consumes monitoring triage (indirectly)
- Produces structured RCA output
"""

from investigation_mock_output import generate_investigation_output


def fetch_investigation_rca(domain: str) -> dict:
    """
    Mock RCA generation.

    Input:
        domain (from MonitoringAgent triage)

    Output:
        RCA structure aligned with Stage 6 PPT
    """
    scenario = (
        "UC_MULTI_DOMAIN_RCA"
        if domain == "CROSS_DOMAIN"
        else "UC_SINGLE_RAN"
    )

    return generate_investigation_output(scenario=scenario)


def publish_investigation_event(payload: dict) -> dict:
    """
    Mock publish (for debugging / logging only)

    In real system:
        - publish_event() handles A2A
        - This layer becomes API call / PubSub push

    Keeping it for symmetry with other agents.
    """
    return {
        "status": "published",
        "target_agent": "SolutionPlanningAgent",
        "payload": payload,
    }