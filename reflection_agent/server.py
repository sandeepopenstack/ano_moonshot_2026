import os, sys, uvicorn
from uuid import uuid4
from google.adk.cli.fast_api import get_fast_api_app

sys.path.insert(0, os.path.dirname(__file__))

from ran_healing_shared.events import (
    NETWORK_STATUS_KEY, EVENT_BUS_KEY, latest_key,
    EVT_EXECUTION_COMPLETED,
)
from reflection_agent.tools import check_execution_result, evaluate_and_publish

parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = get_fast_api_app(agents_dir=parent_dir, web=False, a2a=True)


class _Ctx:
    def __init__(self, state):
        self.state = state


@app.post("/execution-completed")
def reflection_invoke(payload: dict):
    exec_event = {
        "event_id": payload.get("event_id", str(uuid4())),
        "event_type": EVT_EXECUTION_COMPLETED,
        "source": "ExecutorAgent",
        "payload": payload,
        "network_status": "HEALING",
    }
    state = {
        NETWORK_STATUS_KEY: "HEALING",
        EVENT_BUS_KEY: [],
        latest_key(EVT_EXECUTION_COMPLETED): exec_event,
    }
    ctx = _Ctx(state)
    check_execution_result(ctx)
    result = evaluate_and_publish(ctx)
    reflection_output = state.get("reflection_output", {})
    return {
        "status": result[:50] if result else "ok",
        "resolved": reflection_output.get("resolved"),
        "reflection_status": reflection_output.get("status"),
        "reflection_output": reflection_output,
        "network_status": state.get(NETWORK_STATUS_KEY),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
