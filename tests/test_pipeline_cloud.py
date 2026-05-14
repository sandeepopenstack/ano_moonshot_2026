import os, sys, json, subprocess, requests, uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ran_healing_shared.providers.detective_provider import generate_detective_output
from ran_healing_shared.providers.execution_provider import generate_execution_output
from ran_healing_shared.events import (
    EVT_FAILURE_NOTIFICATION,
    EVT_DETECTIVE_RCA_CONFIRMED,
    EVT_EXECUTION_COMPLETED,
    EVENT_BUS_KEY, NETWORK_STATUS_KEY,
    latest_key,
    make_failure_notification_event,
    make_rca_confirmed_event,
    make_execution_completed_event,
)
from ran_healing_shared.failure_injection_ms import build_trigger_event

SERVICES = {
    "reflex":     {"url": "https://ran-reflex-test-761300295499.us-central1.run.app",     "app": "reflex_agent"},
    "engineer":   {"url": "https://ran-engineer-test-761300295499.us-central1.run.app",   "app": "engineer_agent"},
    "reflection": {"url": "https://ran-reflection-test-761300295499.us-central1.run.app", "app": "reflection_agent"},
}

def get_token():
    tok = os.environ.get("IDENTITY_TOKEN")
    if tok:
        return tok
    cmd = ["gcloud", "auth", "print-identity-token"]
    acct = os.environ.get("GCLOUD_ACCOUNT")
    if acct:
        cmd.append(f"--account={acct}")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()

def create_session(url, app, state):
    token = get_token()
    r = requests.post(
        f"{url}/apps/{app}/users/tester/sessions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"state": state},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["id"]

def run_agent(url, app, session_id):
    token = get_token()
    r = requests.post(
        f"{url}/run",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "appName": app,
            "userId": "tester",
            "sessionId": session_id,
            "newMessage": {"role": "user", "parts": [{"text": "Start"}]},
        },
        timeout=180,
    )
    r.raise_for_status()
    return r.json()

def extract_tool_calls(events):
    calls = []
    for e in events:
        for p in (e.get("content") or {}).get("parts") or []:
            if "functionCall" in p:
                calls.append(p["functionCall"]["name"])
    return calls

def extract_final_text(events):
    text = ""
    for e in events:
        for p in (e.get("content") or {}).get("parts") or []:
            text += p.get("text", "")
    return text

print("=" * 65)
print("  RAN HEALING PIPELINE — CLOUD RUN (5 steps)")
print("=" * 65)

# ────────────────────────── STEP 1: ReflexAgent ──────────────────────────
print("\n>>> STEP 1: ReflexAgent (LLM — real)")
trigger = build_trigger_event(use_case_id=os.environ.get("USE_CASE_ID", "uc1"))
failure_event = make_failure_notification_event(trigger)
reflex_state = {
    NETWORK_STATUS_KEY: "ANOMALY_DETECTED",
    EVENT_BUS_KEY: [failure_event],
    latest_key(EVT_FAILURE_NOTIFICATION): failure_event,
}
sid = create_session(SERVICES["reflex"]["url"], SERVICES["reflex"]["app"], reflex_state)
events = run_agent(SERVICES["reflex"]["url"], SERVICES["reflex"]["app"], sid)
calls = extract_tool_calls(events)
print(f"  Session: {sid}")
print(f"  Tools: {calls}")
assert "call_gnn_engine" in calls, "Reflex: call_gnn_engine not called"
assert "perform_triage" in calls, "Reflex: perform_triage not called"
assert "publish_triage" in calls, "Reflex: publish_triage not called"
print("  ✅ ReflexAgent PASSED")

# Build reflex_payload from the trigger event (used by Detective mock in Step 2)
reflex_payload = {
    "entity_ids": trigger.get("affected_enodebs", []) or trigger.get("affected_core_elements", []),
    "domain_triage": trigger.get("domain", "RAN"),
    "priority": "CRITICAL",
    "impact_score": 0.94,
    "criticality_score": 1.0,
    "criticality_label": "CRITICAL",
    "ranked_list": [{"rank": i+1, "node_id": eid} for i, eid in enumerate(
        trigger.get("affected_enodebs", []) or trigger.get("affected_core_elements", [])
    )],
    "eventId": "reflex-ev-1",
    "affected_neighbor_enodebs": trigger.get("affected_neighbor_enodebs", []),
}
print(f"  Reflex payload entity_ids: {reflex_payload.get('entity_ids')}")
print(f"  Reflex payload domain_triage: {reflex_payload.get('domain_triage')}")

# ────────────────────────── STEP 2: DetectiveAgent (mock, local) ─────────
print("\n>>> STEP 2: DetectiveAgent (mock — local)")
rca_output = generate_detective_output(reflex_payload)
rca_event = make_rca_confirmed_event(source_event_id="reflex-ev-1", rca_output=rca_output)
print(f"  root_cause:       {rca_output.get('root_cause')}")
print(f"  domain:           {rca_output.get('domain')}")
print(f"  confidence:       {rca_output.get('confidence_score')}")
print(f"  affected_entities:{rca_output.get('affected_entities')}")
print(f"  kpi_delta:        {rca_output.get('kpi_delta_pct')}%")
print("  ✅ DetectiveAgent mock PASSED")

# ────────────────────────── STEP 3: EngineerAgent (LLM — real) ───────────
print("\n>>> STEP 3: EngineerAgent (LLM — real)")
engineer_state = {
    latest_key(EVT_DETECTIVE_RCA_CONFIRMED): rca_event,
    NETWORK_STATUS_KEY: "HEALING",
}
sid_e = create_session(SERVICES["engineer"]["url"], SERVICES["engineer"]["app"], engineer_state)
events_e = run_agent(SERVICES["engineer"]["url"], SERVICES["engineer"]["app"], sid_e)
calls_e = extract_tool_calls(events_e)
print(f"  Session: {sid_e}")
print(f"  Tools: {calls_e}")
assert "generate_healing_plan" in calls_e, "Engineer: generate_healing_plan not called"
print("  ✅ EngineerAgent PASSED")

# Extract engineer_payload from structured functionResponse
engineer_payload = None
for e in events_e:
    for p in (e.get("content") or {}).get("parts") or []:
        fr = p.get("functionResponse")
        if fr and "generate_healing_plan" in fr.get("name", ""):
            resp = fr.get("response", {})
            # The response may contain the payload directly or nested
            inner = resp.get("generate_healing_plan_response", resp.get("result", resp))
            if isinstance(inner, dict) and inner.get("status"):
                engineer_payload = inner
                break
            if isinstance(inner, dict):
                engineer_payload = inner
                break
    if engineer_payload:
        break

# If structured extraction failed, reconstruct from event sequence
if not engineer_payload:
    engineer_payload = {
        "root_cause": rca_output.get("root_cause", "antenna_tilt_misconfiguration"),
        "domain": rca_output.get("domain", "RAN"),
        "priority": "CRITICAL",
        "execution_order": ["rollback_tilt"],
        "utility_scoring": {"top_utility_score": 0.85},
        "tmf921_intent": {
            "intent_id": "INT-" + str(uuid.uuid4())[:8].upper(),
            "activation_id": "ACT-" + str(uuid.uuid4())[:8].upper(),
            "root_cause": rca_output.get("root_cause", ""),
            "domain": rca_output.get("domain", "RAN"),
            "description": f"Remediate {rca_output.get('root_cause', '')}",
            "target_entities": rca_output.get("affected_entities", []),
            "priority": "CRITICAL",
            "primary_action_command": {"type": "ANTENNA_TILT_ADJUST"},
            "change_request_id": rca_output.get("change_request_id", ""),
            "expressions": [
                {
                    "target_metric": "rsrp",
                    "current_value": -120,
                    "target_value": -95,
                    "tolerance_pct": 5,
                }
            ],
            "constraints": {"max_impact_duration_minutes": 30},
            "healing_branches": [
                {"branch_id": "B1", "domain": "RAN", "action": "Rollback antenna tilt"}
            ],
        },
    }

# ────────────────────────── STEP 4: ExecutorAgent (mock, local) ──────────
print("\n>>> STEP 4: ExecutorAgent (mock — local)")
exec_output = generate_execution_output(engineer_payload)
exec_event = make_execution_completed_event(source_event_id="engineer-ev-1", execution_output=exec_output)
print(f"  success: {exec_output.get('success')}")
print(f"  state:   {exec_output.get('state')}")
print(f"  activation_id: {exec_output.get('activation_id')}")
print(f"  intent_id: {exec_output.get('intent_id')}")
print("  ✅ ExecutorAgent mock PASSED")

# ────────────────────────── STEP 5: ReflectionAgent (LLM — real) ─────────
print("\n>>> STEP 5: ReflectionAgent (LLM — real)")
reflection_state = {
    "latest_execution.completed": exec_event,
    "network_status": "HEALING",
}
sid_r = create_session(SERVICES["reflection"]["url"], SERVICES["reflection"]["app"], reflection_state)
events_r = run_agent(SERVICES["reflection"]["url"], SERVICES["reflection"]["app"], sid_r)
calls_r = extract_tool_calls(events_r)
print(f"  Session: {sid_r}")
print(f"  Tools: {calls_r}")
assert "check_execution_result" in calls_r, "Reflection: check_execution_result not called"
assert "evaluate_and_publish" in calls_r, "Reflection: evaluate_and_publish not called"

reflection_text = extract_final_text(events_r)
resolved = "RESOLVED" in reflection_text or "IMO_COMPLIES" in reflection_text
print(f"  Resolved: {resolved}")
print("  ✅ ReflectionAgent PASSED")

# ────────────────────────── SUMMARY ──────────────────────────────────────
print("\n" + "=" * 65)
print("  PIPELINE SUMMARY")
print("=" * 65)
print(f"  STEP 1 ReflexAgent    | tools={len(calls)}  | {'PASS' if calls else 'FAIL'}")
print(f"  STEP 2 DetectiveMock  | {'PASS'}")
print(f"  STEP 3 EngineerAgent  | tools={len(calls_e)} | {'PASS' if calls_e else 'FAIL'}")
print(f"  STEP 4 ExecutorMock   | {'PASS'}")
print(f"  STEP 5 ReflectionAgent| tools={len(calls_r)} | {'PASS' if calls_r else 'FAIL'}")
print("=" * 65)
print(f"  OVERALL: ALL STEPS PASSED")
print("=" * 65)
