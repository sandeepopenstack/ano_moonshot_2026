import os, sys, json
from dotenv import load_dotenv
load_dotenv()

import requests

BASE_URL = os.environ.get(
    "REFLEX_AGENT_URL",
    "https://reflex-agent-runner-761300295499.us-central1.run.app",
)
TOKEN = os.environ.get("REFLEX_AGENT_TOKEN",
    "eyJhbGciOiJSUzI1NiIsImtpZCI6ImY4ZTY2MjBkMzk3MTFhYTIxY2U4YTJiZjJmM2VlMDFiOTI0Y2IyZDAiLCJ0eXAiOiJKV1QifQ.eyJpc3MiOiJhY2NvdW50cy5nb29nbGUuY29tIiwiYXpwIjoiNjE4MTA0NzA4MDU0LTlyOXMxYzRhbGczNmVybGl1Y2hvOXQ1Mm4zMm42ZGdxLmFwcHMuZ29vZ2xldXNlcmNvbnRlbnQuY29tIiwiYXVkIjoiNjE4MTA0NzA4MDU0LTlyOXMxYzRhbGczNmVybGl1Y2hvOXQ1Mm4zMm42ZGdxLmFwcHMuZ29vZ2xldXNlcmNvbnRlbnQuY29tIiwic3ViIjoiMTE3ODQ1ODIxODUwMzEyODE5MjI5IiwiaGQiOiJ0ZWNobWFoaW5kcmEuY29tIiwiZW1haWwiOiJyb2hpdC5uZWdpQHRlY2htYWhpbmRyYS5jb20iLCJlbWFpbF92ZXJpZmllZCI6dHJ1ZSwiYXRfaGFzaCI6InR5ZlM4UFRIdjBlbldTZnFrTVd2U0EiLCJuYmYiOjE3Nzg1Nzc0MDYsImlhdCI6MTc3ODU3NzcwNiwiZXhwIjoxNzc4NTgxMzA2LCJqdGkiOiIyYzZiY2RlOWNlNzRjNDJlMGM5MWU5NTNhMmU2NzY5NjViY2RkYjJmIn0.HkkUSdvZAlhZueyCZoYwR883VSbInwXTUzvdfR1OSUCR6hOgSM6gVlaDXYFgjYD0f24PNSotBJtU54ooPB-Br-fnHEBJFF6lri8_6Qto7tO-rIag5kVF5bPiKY3zcfqiP3gJSxrrDtP9hOMz4epIaWYnVLsFHQ64fz65OF0oZiUImE15-NDb71XXOzG-5xq_ZHpGIGsOFMwGikX7mQnss_wB_k7ozbchRNdshuP3k4WRgViPF_ud21jCE-atxPmz45ZzXuH3yboSgJ1iud6eWGLrRNpNRT6QQUil0UPPh2jjiulYuJopI-lum3gcDvclviyNwBNtrB_KAQbXgnTXBA",
)
ENDPOINT = f"{BASE_URL.rstrip('/')}/triage"

TRIGGER_PAYLOADS = {
    "uc1": {
        "event": {
            "eventId": "EV-001",
            "trigger": "antenna_tilt_misconfiguration",
            "affected_enodebs": ["eNB-SYN-003", "eNB-SYN-004"],
            "affected_core_elements": [],
            "affected_neighbor_enodebs": ["eNB-SYN-002"],
        }
    },
    "uc2": {
        "event": {
            "eventId": "EV-002",
            "trigger": "hss_failover",
            "affected_enodebs": [],
            "affected_core_elements": ["HSS-SYN-01"],
            "affected_neighbor_enodebs": [],
        }
    },
}


def test_reflex_triage(use_case: str = "uc1") -> dict:
    print("=" * 65)
    print(f"  Reflex Agent Live Test — {use_case}")
    print("=" * 65)

    payload = TRIGGER_PAYLOADS.get(use_case)
    if not payload:
        raise ValueError(f"Unknown use_case: {use_case}")

    print(f"\n  POST {ENDPOINT}")
    print(f"\n  REQUEST:")
    print(json.dumps(payload, indent=4))

    resp = requests.post(
        ENDPOINT,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TOKEN}",
        },
        json=payload,
        timeout=30,
    )

    print(f"\n  RESPONSE: HTTP {resp.status_code}")
    print(json.dumps(resp.json(), indent=4))

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    data = resp.json()

    assert "eventId" in data, "Missing eventId"
    assert "entity_ids" in data, "Missing entity_ids"
    assert "domain_triage" in data, "Missing domain_triage"
    assert "priority_flag" in data, "Missing priority_flag"
    assert "impact_score" in data, "Missing impact_score"
    assert "criticality_score" in data, "Missing criticality_score"
    assert "reference_time" in data, "Missing reference_time"

    assert len(data["entity_ids"]) >= 1, "entity_ids should not be empty"
    assert data["impact_score"] >= 0.0, f"Invalid impact_score: {data['impact_score']}"
    assert data["criticality_score"] >= 0.0, f"Invalid criticality_score: {data['criticality_score']}"

    print(f"\n  All assertions PASSED for {use_case}")
    print("=" * 65)
    return data


def main():
    use_case = os.environ.get("USE_CASE_ID", "uc1")
    passed = 0
    failed = 0

    if use_case == "all":
        cases = ["uc1", "uc2"]
    else:
        cases = [use_case]

    for uc in cases:
        try:
            test_reflex_triage(uc)
            passed += 1
        except Exception as e:
            print(f"\n  FAILED: {uc} — {e}")
            failed += 1

    print(f"\n  Results: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
