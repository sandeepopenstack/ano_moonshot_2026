import asyncio
import json
import os
from dotenv import load_dotenv

load_dotenv()

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.orchestrator.root_agent import root_agent
from app.events import (
    make_gnn_anomaly_event,
    NETWORK_STATUS_KEY,
    EVENT_BUS_KEY,
    EVT_VALIDATION_RESULT,
    latest_key,
    EVT_GNN_ANOMALY_DETECTED,
)
from app.workflow_state import extract_final_summary
from gnn_inference_provider import generate_gnn_inference_event

APP_NAME = "ran_healing_system"
USER_ID  = "gnn_operator"


async def run_pipeline() -> None:
    print("=" * 65)
    print("  RAN Self-Healing — Autonomous Event-Driven Pipeline")
    print("=" * 65)
    print("\n[Stage 1] Network status: HEALTHY")

    # ── Stage 4: GNN anomaly injection ────────────────────────────────────
    # Do this BEFORE creating session so initial state includes the event
    print("\n[Stage 4] GNN detecting anomaly in network...")

    gnn       = generate_gnn_inference_event(scenario=None)
    gnn_event = make_gnn_anomaly_event(gnn)

    print(f"[GNN] Anomaly detected!")
    print(f"      Domain   : {gnn.get('probableDomain')}")
    print(f"      Z-Score  : {gnn.get('anomalyScore', {}).get('zScore')}")
    print(f"      Priority : {gnn.get('businessPriority')}")
    print(f"\n[Network] Status → ANOMALY_DETECTED")

    # ── Session: create with GNN event already in initial state ───────────
    # This avoids needing update_session entirely
    session_service = InMemorySessionService()

    session = await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        state={
            NETWORK_STATUS_KEY:                    "ANOMALY_DETECTED",
            EVENT_BUS_KEY:                         [gnn_event],
            latest_key(EVT_GNN_ANOMALY_DETECTED):  gnn_event,
        }
    )
    print(f"[Session] id={session.id}")

    # ── Runner ────────────────────────────────────────────────────────────
    runner = Runner(
        agent=root_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    trigger = types.Content(
        role="user",
        parts=[types.Part(text="process")]
    )

    print("\n[Pipeline] Autonomous healing started...\n")
    print("-" * 65)

    # ── Autonomous event loop — one ADK run per pipeline stage ────────────
    for _ in range(12):
        async for _ in runner.run_async(
            user_id=USER_ID,
            session_id=session.id,
            new_message=trigger,
        ):
            pass

        # Re-read session state after each stage
        session = await session_service.get_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=session.id,
        )

        bus = session.state.get(EVENT_BUS_KEY, [])
        if not bus:
            break

        latest   = bus[-1]
        evt_type = latest["event_type"]
        print(f"\n EVENT: {evt_type}")

        if evt_type == EVT_VALIDATION_RESULT and latest.get("resolved"):
            print("\n FINAL STATUS: RESOLVED")
            print(" Network restored to HEALTHY")
            break

    print("-" * 65)
    print("\n[Final Summary]")
    print(json.dumps(extract_final_summary(session.state), indent=2))


def main():
    asyncio.run(run_pipeline())


if __name__ == "__main__":
    main()