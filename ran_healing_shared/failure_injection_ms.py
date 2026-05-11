import uuid
from datetime import datetime, timezone


_TRIGGER_EVENTS = {

    "uc1": {
        "trigger": "antenna_tilt_misconfiguration",
        "useCaseId": "uc1",
        "label": "Antenna Tilt Misconfiguration (RAN Coverage Hole)",
        "description": (
            "Simulates a bad RAN parameter push that tilts antennas "
            "on 5 eNodeBs from 4deg to 12deg, creating a coverage hole."
        ),
        "domain": "RAN",
        "affected_layers": ["eNodeB", "Hex Bin"],
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

    "uc2": {
        "trigger": "hss_failover",
        "useCaseId": "uc2",
        "label": "HSS Failover (Core Saturation)",
        "description": (
            "Simulates a Home Subscriber Server failover causing "
            "EMM 17/22 signaling flood on all MMEs."
        ),
        "domain": "CORE",
        "affected_layers": ["HSS", "MME", "eNodeB", "Hex Bin"],
        "affected_core_elements": ["HSS-SYN-01"],
        "affected_enodebs": [],
        "affected_neighbor_enodebs": [],
    },
}


def build_trigger_event(
    use_case_id: str,
    event_id: str | None = None,
    event_time: str | None = None,
) -> dict:
    if use_case_id not in _TRIGGER_EVENTS:
        raise ValueError(f"Unsupported use_case_id={use_case_id}")

    payload = _TRIGGER_EVENTS[use_case_id]

    return {
        "id": event_id or "0001",
        "eventId": f"EV-{uuid.uuid4().hex[:16]}",
        "eventTime": event_time or datetime.now(timezone.utc).isoformat(),
        "eventType": "FailureInjectionCreateEvent",
        "sourceSystem": "FAILURE_INJECTION_MS",
        "probableDomain": "CROSS_DOMAIN",
        "trigger": payload.get("trigger"),
        "useCaseId": payload.get("useCaseId"),
        "label": payload.get("label"),
        "description": payload.get("description"),
        "domain": payload.get("domain"),
        "affected_layers": payload.get("affected_layers", []),
        "affected_core_elements": payload.get("affected_core_elements", []),
        "affected_enodebs": payload.get("affected_enodebs", []),
        "affected_neighbor_enodebs": payload.get("affected_neighbor_enodebs", []),
    }
