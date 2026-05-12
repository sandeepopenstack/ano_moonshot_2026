import os, sys, json, subprocess, requests, time

SERVICES = {
    "reflex": {
        "url": "https://ran-reflex-test-761300295499.us-central1.run.app",
        "app": "reflex_agent",
    },
    "engineer": {
        "url": "https://ran-engineer-test-761300295499.us-central1.run.app",
        "app": "engineer_agent",
    },
    "reflection": {
        "url": "https://ran-reflection-test-761300295499.us-central1.run.app",
        "app": "reflection_agent",
    },
}


def get_token():
    return subprocess.run([
        "gcloud", "auth", "print-identity-token",
        "--account=techm-dev@poc-z-in2300756.iam.gserviceaccount.com",
    ], capture_output=True, text=True, timeout=15).stdout.strip()


def test_agent(name, svc, session_state=None):
    token = get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    print(f"\n{'='*60}")
    print(f"  TEST: {name} ({svc['app']})")
    print(f"{'='*60}")

    r = requests.post(
        f"{svc['url']}/apps/{svc['app']}/users/tester/sessions",
        headers=headers,
        json={"state": session_state or {}},
        timeout=15,
    )
    if r.status_code != 200:
        print(f"  FAIL: create session -> {r.status_code}")
        return False
    session_id = r.json()["id"]
    print(f"  Session: {session_id}")

    r = requests.post(
        f"{svc['url']}/run",
        headers=headers,
        json={
            "appName": svc["app"],
            "userId": "tester",
            "sessionId": session_id,
            "newMessage": {"role": "user", "parts": [{"text": "Start"}]},
        },
        timeout=180,
    )
    if r.status_code != 200:
        print(f"  FAIL: run -> {r.status_code}")
        return False

    events = r.json()
    call_count = sum(
        1 for e in events
        for p in ((e.get("content") or {}).get("parts") or [])
        if "functionCall" in p
    )
    response_count = sum(
        1 for e in events
        for p in ((e.get("content") or {}).get("parts") or [])
        if "functionResponse" in p
    )

    print(f"  Events: {len(events)}")
    print(f"  Tool calls: {call_count}")
    print(f"  Tool responses: {response_count}")

    for e in events:
        parts = (e.get("content") or {}).get("parts") or []
        for p in parts:
            if "functionCall" in p:
                print(f"    -> CALL: {p['functionCall']['name']}")
            if "functionResponse" in p:
                fr = p["functionResponse"]
                resp = json.dumps(fr.get("response", {}))[:120]
                print(f"    <- RESP: {fr['name']} -> {resp}")
            txt = p.get("text", "")
            if txt:
                print(f"    TXT: {txt[:200]}")

    passed = call_count > 0
    print(f"  VERDICT: {'PASS' if passed else 'FAIL'}")
    return passed


def main():
    results = {}
    results["reflex"] = test_agent("ReflexAgent", SERVICES["reflex"])

    results["engineer"] = test_agent("EngineerAgent", SERVICES["engineer"])

    results["reflection"] = test_agent("ReflectionAgent", SERVICES["reflection"])

    print(f"\n{'='*60}")
    print(f"  FINAL SUMMARY")
    print(f"{'='*60}")
    all_pass = True
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  {name.upper():15} {status}")
    print(f"{'='*60}")
    print(f"  OVERALL: {'ALL PASS' if all_pass else 'SOME FAILED'}")
    print(f"{'='*60}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
