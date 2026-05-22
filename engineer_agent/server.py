import os, sys, uvicorn
from uuid import uuid4
from google.adk.cli.fast_api import get_fast_api_app
from log_stream import router as log_router
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, os.path.dirname(__file__))

from ran_healing_shared.events import (
    NETWORK_STATUS_KEY, EVENT_BUS_KEY, latest_key,
    EVT_DETECTIVE_RCA_CONFIRMED,
)
from engineer_agent.tools import generate_healing_plan

parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = get_fast_api_app(agents_dir=parent_dir, web=False, a2a=True)
app.include_router(log_router)
app.add_middleware(                           # for GUI team's browser calls
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

class _Ctx:
    def __init__(self, state):
        self.state = state


@app.post("/rca-confirmed")
def engineer_invoke(payload: dict):
    rca_event = {
        "event_id": payload.get("event_id", str(uuid4())),
        "event_type": EVT_DETECTIVE_RCA_CONFIRMED,
        "source": "DetectiveAgent",
        "payload": payload,
        "network_status": "HEALING",
    }
    state = {
        NETWORK_STATUS_KEY: "HEALING",
        EVENT_BUS_KEY: [],
        latest_key(EVT_DETECTIVE_RCA_CONFIRMED): rca_event,
    }
    ctx = _Ctx(state)
    result = generate_healing_plan(ctx)
    return {
        "status": result.get("status"),
        "engineer_output": state.get("engineer_output", {}),
        "executor_payload": state.get("engineer_output", {}).get("tmf921_intent", {}),
        "network_status": state.get(NETWORK_STATUS_KEY),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
