import os, sys, json, subprocess, requests

BASE_URL = "https://ran-reflex-test-761300295499.us-central1.run.app"


def get_token():
    return subprocess.run([
        "gcloud", "auth", "print-identity-token",
        "--account=techm-dev@poc-z-in2300756.iam.gserviceaccount.com",
    ], capture_output=True, text=True, timeout=15).stdout.strip()


def main():
    token = get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # 1. Create session
    print(">>> Creating session...")
    r = requests.post(
        f"{BASE_URL}/apps/reflex_agent/users/tester/sessions",
        headers=headers,
        json={"state": {"network_status": "ANOMALY_DETECTED"}},
        timeout=15,
    )
    session = r.json()
    session_id = session["id"]
    print(f"    Session: {session_id}")

    # 2. List sessions (verify)
    r = requests.get(
        f"{BASE_URL}/apps/reflex_agent/users/tester/sessions",
        headers=headers, timeout=15,
    )
    print(f"    Sessions: {len(r.json())}")

    # 3. Run the agent
    print(">>> Running ReflexAgent...")
    r = requests.post(
        f"{BASE_URL}/run",
        headers=headers,
        json={
            "appName": "reflex_agent",
            "userId": "tester",
            "sessionId": session_id,
            "newMessage": {
                "role": "user",
                "parts": [{"text": "Start the anomaly detection pipeline"}],
            },
        },
        timeout=120,
    )
    events = r.json()
    print(f"    Events received: {len(events)}")

    # 4. Analyze events
    calls = 0
    responses = 0
    final_text = ""
    for e in events:
        parts = (e.get("content") or {}).get("parts") or []
        for p in parts:
            if "functionCall" in p:
                calls += 1
                print(f"    TOOL CALL: {p['functionCall']['name']}")
            if "functionResponse" in p:
                responses += 1
                fr = p["functionResponse"]
                resp = json.dumps(fr.get("response", {}))
                print(f"    TOOL RESPONSE: {fr['name']}")
                if len(resp) > 200:
                    resp = resp[:200] + "..."
                print(f"      {resp}")
            if p.get("text"):
                final_text += p["text"]

    model_versions = set()
    for e in events:
        mv = e.get("modelVersion")
        if mv:
            model_versions.add(mv)

    print(f"\n>>> RESULTS:")
    print(f"    Model: {', '.join(model_versions) if model_versions else 'N/A'}")
    print(f"    Total events: {len(events)}")
    print(f"    Tool calls: {calls}")
    print(f"    Tool responses: {responses}")
    print(f"    Final text: {final_text[:300] if final_text else '(none)'}")

    if calls > 0:
        print(f"\n>>> VERDICT: PASS \u2014 ReflexAgent invoked tools successfully")
        return 0
    else:
        print(f"\n>>> VERDICT: FAIL \u2014 No tools were called")
        return 1


if __name__ == "__main__":
    sys.exit(main())
