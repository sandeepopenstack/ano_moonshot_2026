"""
failure_injection_ms.py
========================
Failure Injection / Event Trigger MS — STEP 2B ONLY.

STEP 2B — /trigger_event API:
  Sends FailureInjectionCreateEvent directly to ReflexAgent.

Architecture:
  2b  FailureInjectionCreateEvent → ReflexAgent
  4a  ReflexAgent → GNN Inference Engine
  4b  GNN → ReflexAgent
  5   ReflexAgent → MCP / Spanner
  6   DetectiveAgent
  7   EngineerAgent
  8   ExecutorAgent
  10  ReflectionAgent

This mock simulates the external Failure Injection Microservice.

All use cases already contain fully resolved affected entities,
so Step 2A blast-radius calculation is NOT required at runtime.
"""

import uuid
from datetime import datetime, timezone


# =============================================================================
# STEP 2B PAYLOAD DEFINITIONS
# =============================================================================

_TRIGGER_EVENTS = {

    # =========================================================================
    # UC1 — RAN
    # =========================================================================

    "uc1": {

        "trigger": "antenna_tilt_misconfiguration",

        "useCaseId": "uc1",

        "label": "Antenna Tilt Misconfiguration (RAN Coverage Hole)",

        "description": (
            "Simulates a bad RAN parameter push that tilts antennas "
            "on 5 eNodeBs from 4deg to 12deg, creating a coverage hole."
        ),

        "domain": "RAN",

        "affected_layers": [
            "eNodeB",
            "Hex Bin"
        ],

        # FIX 1: corrected typo "affected_core_elemetns" → "affected_core_elements"
        "affected_core_elements": [],

        "affected_enodebs": [
            "eNB-SYN-003",
            "eNB-SYN-004",
            "eNB-SYN-005",
            "eNB-SYN-006",
            "eNB-SYN-007",
        ],

        "affected_neighbor_enodebs": [
            "eNB-SYN-002",
            "eNB-SYN-008",
            "eNB-SYN-009",
        ],
    },

    # =========================================================================
    # UC2 — CORE
    # =========================================================================

    "uc2": {

        "trigger": "hss_failover",

        "useCaseId": "uc2",

        "label": "HSS Failover (Core Saturation)",

        "description": (
            "Simulates a Home Subscriber Server failover causing "
            "EMM 17/22 signaling flood on all MMEs."
        ),

        "domain": "CORE",

        "affected_layers": [
            "HSS",
            "MME",
            "eNodeB",
            "Hex Bin"
        ],

        # FIX 1: corrected typo "affected_core_elemetns" → "affected_core_elements"
        "affected_core_elements": [
            "HSS-SYN-01"
        ],

        "affected_enodebs": [],

        "affected_neighbor_enodebs": [],
    },
}


# =============================================================================
# BUILD STEP 2B EVENT
# =============================================================================

def build_trigger_event(
    use_case_id: str,
    event_id: str | None = None,
    event_time: str | None = None,
) -> dict:
    """
    Build FailureInjectionCreateEvent payload.

    Returns:
        Step 2B payload sent to ReflexAgent.
    """

    if use_case_id not in _TRIGGER_EVENTS:
        raise ValueError(
            f"Unsupported use_case_id={use_case_id}"
        )

    payload = _TRIGGER_EVENTS[use_case_id]

    return {

        # ---------------------------------------------------------------------
        # Event Envelope
        # ---------------------------------------------------------------------

        "id": event_id or "0001",

        "eventId": f"EV-{uuid.uuid4().hex[:16]}",

        "eventTime": (
            event_time
            or datetime.now(timezone.utc).isoformat()
        ),

        "eventType": "FailureInjectionCreateEvent",

        "sourceSystem": "FAILURE_INJECTION_MS",

        "probableDomain": "CROSS_DOMAIN",

        # ---------------------------------------------------------------------
        # Use Case Payload
        # ---------------------------------------------------------------------

        "trigger": payload.get("trigger"),

        "useCaseId": payload.get("useCaseId"),

        "label": payload.get("label"),

        "description": payload.get("description"),

        "domain": payload.get("domain"),

        "affected_layers": payload.get("affected_layers", []),

        # FIX 1: corrected typo "affected_core_elemetns" → "affected_core_elements"
        "affected_core_elements": payload.get("affected_core_elements", []),

        "affected_enodebs": payload.get("affected_enodebs", []),

        "affected_neighbor_enodebs": payload.get(
            "affected_neighbor_enodebs",
            []
        ),
    }


# =============================================================================
# DISPLAY STEP 2B
# =============================================================================

def print_step_2b(trigger_event: dict) -> None:
    """
    Print Step 2B payload.
    """

    import json

    print("\n" + "=" * 70)
    print("[STEP 2B] FailureInjectionMS → ReflexAgent")
    print("=" * 70)

    print("\nAPI CALL")
    print("POST /trigger_event")

    print("\nREQUEST PAYLOAD")

    print(json.dumps({

        "id": trigger_event.get("id"),

        "eventId": trigger_event.get("eventId"),

        "eventTime": trigger_event.get("eventTime"),

        "eventType": trigger_event.get("eventType"),

        "sourceSystem": trigger_event.get("sourceSystem"),

        "probableDomain": trigger_event.get("probableDomain"),

        "trigger": trigger_event.get("trigger"),

        "useCaseId": trigger_event.get("useCaseId"),

        "label": trigger_event.get("label"),

        "description": trigger_event.get("description"),

        "domain": trigger_event.get("domain"),

        "affected_layers": trigger_event.get("affected_layers"),

        # FIX 1: corrected typo "affected_core_elemetns" → "affected_core_elements"
        "affected_core_elements": trigger_event.get("affected_core_elements"),

        "affected_enodebs": trigger_event.get("affected_enodebs"),

        "affected_neighbor_enodebs": trigger_event.get(
            "affected_neighbor_enodebs"
        ),

    }, indent=4))

    print("\nAPI RESPONSE")
    print("HTTP 200 OK")

    print("\nRESPONSE PAYLOAD")

    print(json.dumps({

        "status": "accepted",

        "eventId": trigger_event.get("eventId"),

        "eventType": trigger_event.get("eventType"),

        "message": (
            "FailureInjectionCreateEvent "
            "received by ReflexAgent"
        )

    }, indent=4))

    print("=" * 70)