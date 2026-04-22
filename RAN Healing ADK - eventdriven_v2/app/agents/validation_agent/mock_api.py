"""
app/agents/validation_agent/mock_api.py
==========================================
Stage 10 → Stage 11 PUBLISH.

PUBLISH: send validation verdict to GUI dashboard and Cloud Logging.
         mock  → receipt dict + console print (visible in demo)
         prod  → POST to GUI WebSocket / REST, Cloud Logging structured write,
                 close/reopen incident ticket in NMC

No FETCH needed — ValidationAgent reads execution result from session state.
"""


def publish_validation_result(validation_output: dict) -> dict:
    """
    Stage 10 → Stage 11.
    Publish validation verdict to GUI dashboard and Cloud Logging.

    Production replacement:
      - POST to GUI dashboard WebSocket / REST endpoint
      - Cloud Logging structured log write (Stage 11)
      - Close or reopen incident ticket in NMC
    """
    resolved         = validation_output.get("status") == "IMO_COMPLIES"
    gui_status       = validation_output.get("gui_status", "UNKNOWN")
    retrigger_count  = validation_output.get("retrigger_count", 0)
    post_z           = validation_output.get("post_action_score")

    print(f"[ValidationAgent mock_api] GUI update: {gui_status} | "
          f"resolved={resolved} | "
          f"post_z={post_z} | "
          f"retrigger_count={retrigger_count}")

    return {
        "status":           "published",
        "target":           "GUI_DASHBOARD",
        "event_type":       "validation.result",
        "gui_status":       gui_status,
        "resolved":         resolved,
        "network_status":   "RESOLVED" if resolved else "ANOMALY_DETECTED",
        "logged_to_cloud":  True,
    }
