import os, sys, uvicorn, asyncio, json
from uuid import uuid4
from google.adk.cli.fast_api import get_fast_api_app
from reflex_agent.log_stream import router as log_router
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
sys.path.insert(0, os.path.dirname(__file__))
from ran_healing_shared.events import (
    NETWORK_STATUS_KEY, EVENT_BUS_KEY, latest_key,
    make_failure_notification_event,
)
from ran_healing_shared.failure_injection_ms import build_trigger_event
from reflex_agent.tools import call_gnn_engine, perform_triage, publish_triage
from reflex_agent.step_events import emit_step, _step_queues  # SSE queue lives here

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
    """
    GUI connects here before or after /trigger_event is called.
    Receives JSON step events as the pipeline executes.

    GUI usage:
      const es = new EventSource(
        `https://ran-reflex-test-v2-xxx.run.app/step-stream/${eventId}`
      );
      es.onmessage = e => {
        const {step, status, meta, payload} = JSON.parse(e.data);
        updateStepDot(step, status, meta, payload);
      };
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    _step_queues[event_id] = q

    async def generate():
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=60)
                    yield f"data: {json.dumps(msg)}\n\n"
                    if msg.get("step") == "detective_called" and msg.get("status") in ("done", "error"):
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
    uvicorn.run(app, host="0.0.0.0", port=8080)