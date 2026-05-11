"""
main.py
========
Pipeline entry point.

Starts directly from:
  STEP 2B — FailureInjectionCreateEvent

Flow:
  2b  FailureInjectionMS -> ReflexAgent
  4a  ReflexAgent -> GNN
  4b  GNN -> ReflexAgent
  5   ReflexAgent -> MCP / Spanner
  6   DetectiveAgent (external Ericsson env)
  7   EngineerAgent
  8   ExecutorAgent (external Ericsson env)
  10  ReflectionAgent
"""

import asyncio
import json
import logging
import os

from dotenv import load_dotenv

load_dotenv()

# -----------------------------------------------------------------------------
# Force ADK model
# -----------------------------------------------------------------------------

os.environ["AGENT_MODEL"] = "gemini-2.5-flash"

# -----------------------------------------------------------------------------

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.orchestrator.root_agent import root_agent

from app.events import (
    NETWORK_STATUS_KEY,
    EVENT_BUS_KEY,
    make_failure_notification_event,
    EVT_FAILURE_NOTIFICATION,
    latest_key,
)

from app.workflow_state import extract_final_summary

# -----------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)

MAX_LOOPS = int(os.environ.get("MAX_PIPELINE_LOOPS", 12))

APP_NAME = "ran_healing_app"
USER_ID = "local_user"

# -----------------------------------------------------------------------------
# Dynamic Use Case Selection
# -----------------------------------------------------------------------------

USE_CASE_ID = os.environ.get("USE_CASE_ID", "uc1")

# -----------------------------------------------------------------------------
# STEP 2B INPUT PAYLOADS
# -----------------------------------------------------------------------------
TRIGGER_EVENTS = {

    # -------------------------------------------------------------------------
    # UC1 — Antenna Tilt Misconfiguration
    # -------------------------------------------------------------------------

    "uc1": {
        "id": "0001",
        "eventId": "EV-dekfn_efjnf_fefe",
        "eventTime": "2024-03-20T10:15:30Z",
        "eventType": "FailureInjectionCreateEvent",
        "sourceSystem": "FAILURE_INJECTION_MS",
        "probableDomain": "CROSS_DOMAIN",
        "trigger": "antenna_tilt_misconfiguration",
        "useCaseId": "uc1",
        "domain": "RAN",
        "affected_layers": [
            "eNodeB",
            "Hex Bin"
        ],
        "affected_core_elements": [],
        "affected_enodebs": [
            "eNB-SYN-003",
            "eNB-SYN-004",
            "eNB-SYN-005",
            "eNB-SYN-006",
            "eNB-SYN-007"
        ],
        "affected_neighbor_enodebs": [
            "eNB-SYN-002",
            "eNB-SYN-008",
            "eNB-SYN-009"
        ]
    },

    # -------------------------------------------------------------------------
    # UC2 — HSS Failover
    # -------------------------------------------------------------------------

    "uc2": {
        "id": "0002",
        "eventId": "EV-dekfn_efjnf_gege",
        "eventTime": "2024-03-20T10:15:30Z",
        "eventType": "FailureInjectionCreateEvent",
        "sourceSystem": "FAILURE_INJECTION_MS",
        "probableDomain": "CROSS_DOMAIN",
        "trigger": "hss_failover",
        "useCaseId": "uc2",
        "domain": "CORE",
        "affected_layers": [
            "HSS",
            "MME",
            "eNodeB",
            "Hex Bin"
        ],
        "affected_core_elements": [
            "HSS-SYN-01"
        ],
        "affected_enodebs": [],
        "affected_neighbor_enodebs": []
    }
}
# -----------------------------------------------------------------------------

if USE_CASE_ID not in TRIGGER_EVENTS:
    raise ValueError(
        f"Unsupported USE_CASE_ID={USE_CASE_ID}"
    )

# -----------------------------------------------------------------------------


async def run_pipeline() -> None:

    print("=" * 70)
    print("           RAN SELF-HEALING PIPELINE")
    print("=" * 70)

    print(f"Model       : {os.environ.get('AGENT_MODEL')}")
    print(f"Use Case ID : {USE_CASE_ID}")

    vertex = os.environ.get(
        "GOOGLE_GENAI_USE_VERTEXAI",
        "false"
    ).lower()

    print(
        f"Backend     : "
        f"{'Vertex AI (GCP)' if vertex == 'true' else 'Gemini API'}"
    )

    print("=" * 70)

    # =========================================================================
    # STEP 2B — FailureInjectionCreateEvent
    # =========================================================================

    trigger_payload = TRIGGER_EVENTS[USE_CASE_ID]

    print("\n" + "=" * 70)
    print("[STEP 2B — Failure Injection MS → ReflexAgent]")
    print("=" * 70)

    print("\nAPI CALL")
    print("POST /trigger_event")

    print("\nREQUEST PAYLOAD")
    print(json.dumps(trigger_payload, indent=2))

    print("\nRESPONSE")

    print(json.dumps({
        "status": "RECEIVED",
        "next_target": "ReflexAgent",
        "event_type": "FailureInjectionCreateEvent",
        "http_status": 200
    }, indent=2))

    # =========================================================================
    # Initial Session State
    # =========================================================================

    failure_event = make_failure_notification_event(trigger_payload)

    initial_state = {
        NETWORK_STATUS_KEY: "ANOMALY_DETECTED",
        EVENT_BUS_KEY: [failure_event],
        latest_key(EVT_FAILURE_NOTIFICATION): failure_event,
    }

    # =========================================================================
    # ADK Session
    # =========================================================================

    session_service = InMemorySessionService()

    session = await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        state=initial_state,
    )

    # =========================================================================
    # ADK Runner
    # =========================================================================

    runner = Runner(
        app_name=APP_NAME,
        agent=root_agent,
        session_service=session_service,
    )

    trigger = types.Content(
        role="user",
        parts=[types.Part(text="process")]
    )

    # =========================================================================
    # PIPELINE LOOP
    # =========================================================================

    for loop_num in range(MAX_LOOPS):

        print("\n" + "-" * 70)
        print(f"PIPELINE LOOP {loop_num + 1}")
        print("-" * 70)

        try:

            async for _ in runner.run_async(
                session_id=session.id,
                user_id=USER_ID,
                new_message=trigger,
            ):
                pass

        except Exception as e:

            error_str = str(e)

            print("\n[PIPELINE ERROR]")
            print(error_str)

            if any(code in error_str for code in [
                "429",
                "503",
                "RESOURCE_EXHAUSTED",
                "UNAVAILABLE",
            ]):
                print("\n[RateLimit] Waiting 60s before retry...")
                await asyncio.sleep(60)
                continue

            raise

        # ---------------------------------------------------------------------

        session = await session_service.get_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=session.id,
        )

        network_status = session.state.get(
            NETWORK_STATUS_KEY,
            "UNKNOWN"
        )

        # Only print status when it is a terminal state (RESOLVED or FAILED)
        # HEALING is an intermediate state and does not need to be shown
        if network_status in ("RESOLVED", "FAILED", "UNKNOWN"):
            print(f"\n[Loop {loop_num + 1}] Network Status: {network_status}")

        if network_status == "RESOLVED":

            print("\n" + "=" * 70)
            print("FINAL STATUS: RESOLVED — NETWORK HEALED")
            print("=" * 70)

            break

    # =========================================================================
    # FINAL SUMMARY
    # =========================================================================

    print("\n" + "=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)

    final_summary = extract_final_summary(session.state)

    print(json.dumps(final_summary, indent=2))


# -----------------------------------------------------------------------------


def main():
    asyncio.run(run_pipeline())


# -----------------------------------------------------------------------------

if __name__ == "__main__":
    main()