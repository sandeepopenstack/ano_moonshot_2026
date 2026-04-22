"""
main.py — RAN Self-Healing Pipeline Entry Point
=================================================
Runs the full autonomous healing pipeline:
  Stage 1  : Healthy network (baseline)
  Stage 4  : GNN injects anomaly
  Stage 5  : MonitoringAgent triages
  Stage 6  : InvestigationAgent (mock) confirms RCA
  Stage 7  : SolutionPlanningAgent builds TMF921 intent
  Stage 9  : ExecutionAgent (mock) applies fix
  Stage 10 : ValidationAgent checks pre/post Z-score
  → RESOLVED or retrigger (max 3 retries from VALIDATION_CONFIG)
"""

import asyncio
import json
import os
import sys

# ── Import path fix for Cloud Run ─────────────────────────────────────────────
# gnn_inference_provider.py lives at the project root.
# sys.path.insert(0, '.') is unreliable in Cloud Run — resolve explicitly.
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

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
    EVT_GNN_ANOMALY_DETECTED,
    latest_key,
)
from app.workflow_state import extract_final_summary
from gnn_inference_provider import generate_gnn_inference_event  # noqa: E402

APP_NAME = "ran_healing_system"
USER_ID  = "gnn_operator"

# Max loop: 6 stages × (1 initial + 3 retries) = 24, plus buffer
_MAX_LOOP = 30


async def run_pipeline() -> None:
    print("=" * 65)
    print("  RAN Self-Healing — Autonomous Event-Driven Pipeline")
    print("=" * 65)
    print("\n[Stage 1] Network status: HEALTHY")

    # ── Stage 4: GNN detects anomaly ──────────────────────────────────────
    print("\n[Stage 4] GNN inference engine scanning network topology...")

    gnn       = generate_gnn_inference_event(scenario=None)   # random UC1/UC2/UC3
    gnn_event = make_gnn_anomaly_event(gnn)

    print(f"[GNN] Anomaly detected!")
    print(f"      Domain   : {gnn.get('probableDomain')}")
    print(f"      Z-Score  : {gnn.get('anomalyScore', {}).get('zScore')}")
    print(f"      Priority : {gnn.get('businessPriority')}")
    print(f"\n[Network] Status → ANOMALY_DETECTED")

    # ── Create session with GNN event pre-loaded in state ─────────────────
    session_service = InMemorySessionService()

    session = await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        state={
            NETWORK_STATUS_KEY:                   "ANOMALY_DETECTED",
            EVENT_BUS_KEY:                        [gnn_event],
            latest_key(EVT_GNN_ANOMALY_DETECTED): gnn_event,
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

    # ── Autonomous event loop ─────────────────────────────────────────────
    # Each iteration: orchestrator reads latest event → routes to tool →
    # tool writes next event → loop continues.
    # On retrigger: ValidationAgent writes a fresh gnn.anomaly.detected;
    # next iteration routes it back to MonitoringAgent automatically.
    for iteration in range(_MAX_LOOP):
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
            print("[Pipeline] Event bus empty — pipeline complete")
            break

        latest   = bus[-1]
        evt_type = latest["event_type"]
        network  = session.state.get(NETWORK_STATUS_KEY, "UNKNOWN")
        print(f"\n[Loop {iteration + 1:02d}] EVENT: {evt_type}  |  {network}")

        if evt_type == EVT_VALIDATION_RESULT and latest.get("resolved"):
            print("\n FINAL STATUS: RESOLVED")
            print(" Network restored to HEALTHY")
            break
    else:
        print(f"\n Loop limit ({_MAX_LOOP}) reached — check retrigger count in logs")

    print("-" * 65)
    print("\n[Final Summary]")
    print(json.dumps(extract_final_summary(session.state), indent=2))


def main():
    asyncio.run(run_pipeline())


if __name__ == "__main__":
    main()
