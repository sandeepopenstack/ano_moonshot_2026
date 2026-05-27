import os, sys, uvicorn, asyncio, json
from uuid import uuid4
from google.adk.cli.fast_api import get_fast_api_app
from engineer_agent.log_stream import router as log_router
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
sys.path.insert(0, os.path.dirname(__file__))
from ran_healing_shared.events import (
    NETWORK_STATUS_KEY, EVENT_BUS_KEY, latest_key,
    EVT_DETECTIVE_RCA_CONFIRMED,
)
from engineer_agent.tools import generate_healing_plan
from engineer_agent.step_events import emit_step, _step_queues  # SSE queue lives here

parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = get_fast_api_app(agents_dir=parent_dir, web=False, a2a=True)
app.include_router(log_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ── SSE endpoint ───────────────────────────────────────────────────────────────
@app.get("/step-stream/{event_id}")
async def step_stream(event_id: str):
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    _step_queues[event_id] = q

    async def generate():
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=60)
                    yield f"data: {json.dumps(msg)}\n\n"
                    if msg.get("step") == "executor_called" and msg.get("status") in ("done", "error"):
                        yield f"data: {json.dumps({'step':'__done__','status':'done'})}\n\n"
                        break
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _step_queues.pop(event_id, None)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ── existing endpoint — UNCHANGED ─────────────────────────────────────────────
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
    uvicorn.run(app, host="0.0.0.0", port=8080)