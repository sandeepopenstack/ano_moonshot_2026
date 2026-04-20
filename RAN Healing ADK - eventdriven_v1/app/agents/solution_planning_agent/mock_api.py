from investigation_mock_output import (
    generate_investigation_output,
    publish_investigation_output
)


def fetch_investigation_rca():
    """
    Consume Stage 6 published RCA output.
    """
    published = publish_investigation_output(
        generate_investigation_output()
    )
    return published["payload"]


def publish_execution_handoff(payload: dict):
    """
    Mock downstream handoff for execution team integration.
    """
    return {
        "status": "published",
        "target_agent": "ExecutionAgent",
        "payload": payload
    }