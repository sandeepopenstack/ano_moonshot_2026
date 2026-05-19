import os, sys, uvicorn
from uuid import uuid4
from google.adk.cli.fast_api import get_fast_api_app

sys.path.insert(0, os.path.dirname(__file__))

from ran_healing_shared.events import (
    NETWORK_STATUS_KEY, EVENT_BUS_KEY, latest_key,
    make_failure_notification_event,
)
from ran_healing_shared.failure_injection_ms import build_trigger_event
from reflex_agent.tools import call_gnn_engine, perform_triage, publish_triage

parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = get_fast_api_app(agents_dir=parent_dir, web=False, a2a=True)


class _Ctx:
    def __init__(self, state):
        self.state = state


@app.post("/trigger_event")
def reflex_invoke(payload: dict = {}):
    if payload and ("trigger" in payload or "affected_enodebs" in payload or "affected_core_elements" in payload):
        trigger = payload
    else:
        use_case = payload.get("use_case_id", "uc1")
        trigger = build_trigger_event(use_case_id=use_case)
    failure = make_failure_notification_event(trigger)
    state = {
        NETWORK_STATUS_KEY: "ANOMALY_DETECTED",
        EVENT_BUS_KEY: [failure],
        latest_key(failure["event_type"]): failure,
    }
    ctx = _Ctx(state)
    call_gnn_engine(ctx)
    perform_triage(ctx)
    publish_triage(ctx)
    reflex_event = state.get("reflex_output", {})
    return {
        "status": "ok",
        "network_status": state.get(NETWORK_STATUS_KEY),
        "detective_investigation_request": reflex_event.get("payload", {}),
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
