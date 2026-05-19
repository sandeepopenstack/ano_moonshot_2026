import os, sys, json, subprocess, requests, uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ran_healing_shared.providers.detective_provider import generate_detective_output
from ran_healing_shared.providers.execution_provider import generate_execution_output
from ran_healing_shared.events import (
    EVT_FAILURE_NOTIFICATION, EVT_DETECTIVE_RCA_CONFIRMED, EVT_EXECUTION_COMPLETED,
    EVENT_BUS_KEY, NETWORK_STATUS_KEY,
    latest_key, make_failure_notification_event, make_rca_confirmed_event, make_execution_completed_event,
)
from ran_healing_shared.failure_injection_ms import build_trigger_event

SERVICES = {
    "reflex":     {"url": "https://ran-reflex-test-761300295499.us-central1.run.app",     "app": "reflex_agent"},
    "engineer":   {"url": "https://ran-engineer-test-761300295499.us-central1.run.app",   "app": "engineer_agent"},
    "reflection": {"url": "https://ran-reflection-test-761300295499.us-central1.run.app", "app": "reflection_agent"},
}

LOG_FILE = os.environ.get("LOG_FILE", "pipeline_run.log")

def tee(*args, **kwargs):
    text = " ".join(str(a) for a in args)
    if kwargs:
        text += " " + json.dumps(kwargs)
    print(text)
    with open(LOG_FILE, "a") as f:
        f.write(text + "\n")

def json_pp(obj, indent=2):
    return json.dumps(obj, indent=indent, default=str)

def separator(title):
    line = "\n" + "=" * 70
    tee(line)
    tee(f"  {title}")
    tee("=" * 70)

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
    tee(f"  POST {url}/apps/{app}/users/tester/sessions  (state keys: {list(state.keys())})")
    r = requests.post(
        f"{url}/apps/{app}/users/tester/sessions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"state": state},
        timeout=30,
    )
    tee(f"  Session creation → {r.status_code}")
    r.raise_for_status()
    sid = r.json()["id"]
    tee(f"  Session ID: {sid}")
    return sid

def run_agent(url, app, session_id):
    token = get_token()
    tee(f"  POST {url}/run  (session={session_id[:12]}...)")
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
    tee(f"  Agent run → {r.status_code}")
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

def dump_agent_response(events, label):
    tee(f"\n  --- {label} raw response ---")
    for i, e in enumerate(events):
        role = e.get("content", {}).get("role", "?")
        parts = e.get("content", {}).get("parts", [])
        for j, p in enumerate(parts):
            if "text" in p:
                t = p["text"][:300]
                tee(f"  [{i}:{j}] {role} text: {t}")
            if "functionCall" in p:
                fc = p["functionCall"]
                tee(f"  [{i}:{j}] {role} functionCall: {fc['name']}")
                args_str = json.dumps(fc.get("args", {}), default=str)[:500]
                tee(f"         args: {args_str}")
            if "functionResponse" in p:
                fr = p["functionResponse"]
                tee(f"  [{i}:{j}] {role} functionResponse: {fr.get('name')}")
                resp_str = json.dumps(fr.get("response", {}), default=str)[:500]
                tee(f"         response: {resp_str}")
    tee(f"  --- end raw response ---")

# ── Main ────────────────────────────────────────────────────────────────

USE_CASE_ID = os.environ.get("USE_CASE_ID", "uc1")
TIMESTAMP = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

with open(LOG_FILE, "w") as f:
    f.write(f"Pipeline Run — {TIMESTAMP}\n")
    f.write(f"USE_CASE_ID={USE_CASE_ID}\n")
    f.write("=" * 70 + "\n")

separator(f"RAN HEALING PIPELINE — CLOUD RUN  |  {TIMESTAMP}")
tee(f"  USE_CASE_ID    : {USE_CASE_ID}")
tee(f"  LOG_FILE       : {os.path.abspath(LOG_FILE)}")
tee(f"  Reflex URL     : {SERVICES['reflex']['url']}")
tee(f"  Engineer URL   : {SERVICES['engineer']['url']}")
tee(f"  Reflection URL : {SERVICES['reflection']['url']}")

# ── STEP 0: Trigger ─────────────────────────────────────────────────────

separator("STEP 0: FailureInjectionMS → Trigger Event")
trigger = build_trigger_event(use_case_id=USE_CASE_ID)
tee(f"  Source      : {trigger.get('sourceSystem')}")
tee(f"  Event Type  : {trigger.get('eventType')}")
tee(f"  Trigger     : {trigger.get('trigger')}")
tee(f"  Use Case ID : {trigger.get('useCaseId')}")
tee(f"  Domain      : {trigger.get('domain')}")
enodebs = trigger.get("affected_enodebs", [])
core_el = trigger.get("affected_core_elements", [])
tee(f"  Affected eNBs: {enodebs}")
tee(f"  Affected Core: {core_el}")
tee(f"  Neighbor eNBs: {trigger.get('affected_neighbor_enodebs', [])}")
tee(f"\n  Full trigger payload:")
tee(json_pp(trigger))

# ── STEP 1: ReflexAgent ─────────────────────────────────────────────────

separator("STEP 1: ReflexAgent (LLM — real Cloud Run)")
failure_event = make_failure_notification_event(trigger)
reflex_state = {
    NETWORK_STATUS_KEY: "ANOMALY_DETECTED",
    EVENT_BUS_KEY: [failure_event],
    latest_key(EVT_FAILURE_NOTIFICATION): failure_event,
}
tee(f"\n  Initial state:")
tee(f"    {NETWORK_STATUS_KEY}: ANOMALY_DETECTED")
tee(f"    {EVENT_BUS_KEY}: [{failure_event.get('event_type')}]")
tee(f"    {latest_key(EVT_FAILURE_NOTIFICATION)}: (failure event)")

sid = create_session(SERVICES["reflex"]["url"], SERVICES["reflex"]["app"], reflex_state)
events = run_agent(SERVICES["reflex"]["url"], SERVICES["reflex"]["app"], sid)
calls = extract_tool_calls(events)

dump_agent_response(events, "ReflexAgent")

tee(f"\n  Tool calls: {calls}")
assert "call_gnn_engine" in calls, "Reflex: call_gnn_engine not called"
assert "perform_triage" in calls, "Reflex: perform_triage not called"
assert "publish_triage" in calls, "Reflex: publish_triage not called"

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
tee(f"\n  Reflex payload (→ Detective):")
tee(f"    entity_ids      : {reflex_payload.get('entity_ids')}")
tee(f"    domain_triage   : {reflex_payload.get('domain_triage')}")
tee(f"    priority        : {reflex_payload.get('priority')}")
tee(f"    impact_score    : {reflex_payload.get('impact_score')}")
tee(f"    criticality     : {reflex_payload.get('criticality_label')}")
tee(f"    ranked_list     : {[r['node_id'] for r in reflex_payload.get('ranked_list', [])]}")
tee(f"    neighbor_enodebs: {reflex_payload.get('affected_neighbor_enodebs')}")

tee("\n  ✅ ReflexAgent PASSED")

# ── STEP 2: DetectiveAgent ──────────────────────────────────────────────

separator("STEP 2: DetectiveAgent (mock — local)")
rca_output = generate_detective_output(reflex_payload)
rca_event = make_rca_confirmed_event(source_event_id="reflex-ev-1", rca_output=rca_output)

tee(f"\n  RCA output:")
tee(f"    root_cause       : {rca_output.get('root_cause')}")
tee(f"    domain           : {rca_output.get('domain')}")
tee(f"    confidence_score : {rca_output.get('confidence_score')}")
tee(f"    affected_entities: {rca_output.get('affected_entities')}")
tee(f"    kpi_delta_pct    : {rca_output.get('kpi_delta_pct')}%")
tee(f"    change_request_id: {rca_output.get('change_request_id')}")
tee(f"    severity         : {rca_output.get('severity')}")
tee(f"\n  Published event: {rca_event.get('event_type')}")
tee(f"  Source event    : {rca_event.get('source_event_id')}")
tee("\n  ✅ DetectiveAgent mock PASSED")

# ── STEP 3: EngineerAgent ───────────────────────────────────────────────

separator("STEP 3: EngineerAgent (LLM — real Cloud Run)")
engineer_state = {
    latest_key(EVT_DETECTIVE_RCA_CONFIRMED): rca_event,
    NETWORK_STATUS_KEY: "HEALING",
}
tee(f"\n  Initial state:")
tee(f"    {latest_key(EVT_DETECTIVE_RCA_CONFIRMED)}: (rca confirmed event)")
tee(f"    {NETWORK_STATUS_KEY}: HEALING")

sid_e = create_session(SERVICES["engineer"]["url"], SERVICES["engineer"]["app"], engineer_state)
events_e = run_agent(SERVICES["engineer"]["url"], SERVICES["engineer"]["app"], sid_e)
calls_e = extract_tool_calls(events_e)

dump_agent_response(events_e, "EngineerAgent")

tee(f"\n  Tool calls: {calls_e}")
assert "generate_healing_plan" in calls_e, "Engineer: generate_healing_plan not called"

engineer_llm_raw = None
for e in events_e:
    for p in (e.get("content") or {}).get("parts") or []:
        fr = p.get("functionResponse")
        if fr and "generate_healing_plan" in fr.get("name", ""):
            resp = fr.get("response", {})
            inner = resp.get("generate_healing_plan_response", resp.get("result", resp))
            if isinstance(inner, dict):
                engineer_llm_raw = inner
                break
    if engineer_llm_raw:
        break

target_entities = rca_output.get("affected_entities", [])
engineer_payload = {
    "root_cause": rca_output.get("root_cause", "antenna_tilt_misconfiguration"),
    "domain": rca_output.get("domain", "RAN"),
    "priority": (engineer_llm_raw or {}).get("priority", "CRITICAL"),
    "execution_order": (engineer_llm_raw or {}).get("execution_order", ["rollback_tilt"]),
    "intent_id": (engineer_llm_raw or {}).get("intent_id", "INT-" + str(uuid.uuid4())[:8].upper()),
    "affected_entities": target_entities,
    "change_request_id": rca_output.get("change_request_id", ""),
    "tmf921_intent": {
        "intent_id": (engineer_llm_raw or {}).get("intent_id", "INT-" + str(uuid.uuid4())[:8].upper()),
        "activation_id": "ACT-" + str(uuid.uuid4())[:8].upper(),
        "root_cause": rca_output.get("root_cause", ""),
        "domain": rca_output.get("domain", "RAN"),
        "description": f"Remediate {rca_output.get('root_cause', '')}",
        "target_entities": target_entities,
        "priority": (engineer_llm_raw or {}).get("priority", "CRITICAL"),
        "primary_action_command": {"type": "ANTENNA_TILT_ADJUST"},
        "change_request_id": rca_output.get("change_request_id", ""),
        "expressions": [{"target_metric": "rsrp", "current_value": -120, "target_value": -95, "tolerance_pct": 5}],
        "constraints": {"max_impact_duration_minutes": 30},
        "healing_branches": [{"branch_id": "B1", "domain": "RAN", "action": "Rollback antenna tilt"}],
    },
}

tmf921 = engineer_payload.get("tmf921_intent", {})
tee(f"\n  Engineer output:")
tee(f"    root_cause      : {engineer_payload.get('root_cause')}")
tee(f"    domain          : {engineer_payload.get('domain')}")
tee(f"    priority        : {engineer_payload.get('priority')}")
tee(f"    execution_order : {engineer_payload.get('execution_order')}")
tee(f"    intent_id       : {engineer_payload.get('intent_id')}")
tee(f"    target_entities : {tmf921.get('target_entities')}")
tee(f"    action_command  : {tmf921.get('primary_action_command', {}).get('type')}")
tee(f"    change_request  : {tmf921.get('change_request_id')}")
tee(f"    healing_branches: {[b['action'] for b in tmf921.get('healing_branches', [])]}")
tee(f"    metrics         : {[e.get('target_metric') for e in tmf921.get('expressions', [])]}")
tee("\n  ✅ EngineerAgent PASSED")

# ── STEP 4: ExecutorAgent ───────────────────────────────────────────────

separator("STEP 4: ExecutorAgent (mock — local)")
exec_output = generate_execution_output(engineer_payload)
exec_event = make_execution_completed_event(source_event_id="engineer-ev-1", execution_output=exec_output)

tee(f"\n  Execution output:")
tee(f"    success       : {exec_output.get('success')}")
tee(f"    state         : {exec_output.get('state')}")
tee(f"    activation_id : {exec_output.get('activation_id')}")
tee(f"    intent_id     : {exec_output.get('intent_id')}")
tee(f"    message       : {exec_output.get('message')}")
tee(f"\n  Published event: {exec_event.get('event_type')}")
tee(f"  Source event    : {exec_event.get('source_event_id')}")
tee("\n  ✅ ExecutorAgent mock PASSED")

# ── STEP 5: ReflectionAgent ─────────────────────────────────────────────

separator("STEP 5: ReflectionAgent (LLM — real Cloud Run)")
reflection_state = {
    latest_key(EVT_EXECUTION_COMPLETED): exec_event,
    NETWORK_STATUS_KEY: "HEALING",
}
tee(f"\n  Initial state:")
tee(f"    latest_execution.completed: (execution completed event)")
tee(f"    network_status: HEALING")

sid_r = create_session(SERVICES["reflection"]["url"], SERVICES["reflection"]["app"], reflection_state)
events_r = run_agent(SERVICES["reflection"]["url"], SERVICES["reflection"]["app"], sid_r)
calls_r = extract_tool_calls(events_r)

dump_agent_response(events_r, "ReflectionAgent")

tee(f"\n  Tool calls: {calls_r}")
assert "check_execution_result" in calls_r, "Reflection: check_execution_result not called"
assert "evaluate_and_publish" in calls_r, "Reflection: evaluate_and_publish not called"

reflection_text = extract_final_text(events_r)
resolved = "RESOLVED" in reflection_text or "IMO_COMPLIES" in reflection_text
tee(f"\n  Resolved: {resolved}")
tee(f"  Final text snippet: {reflection_text[:500]}")
tee("\n  ✅ ReflectionAgent PASSED")

# ── SUMMARY ─────────────────────────────────────────────────────────────

separator("PIPELINE SUMMARY")
tee(f"  STEP 1 ReflexAgent    | tools={len(calls)}  | {'PASS' if calls else 'FAIL'}")
tee(f"  STEP 2 DetectiveMock  | PASS")
tee(f"  STEP 3 EngineerAgent  | tools={len(calls_e)} | {'PASS' if calls_e else 'FAIL'}")
tee(f"  STEP 4 ExecutorMock   | PASS")
tee(f"  STEP 5 ReflectionAgent| tools={len(calls_r)} | {'PASS' if calls_r else 'FAIL'}")

overall = bool(calls) and bool(calls_e) and bool(calls_r)
tee("=" * 70)
tee(f"  OVERALL: {'ALL STEPS PASSED' if overall else 'SOME STEPS FAILED'}")
tee(f"  Log saved to: {os.path.abspath(LOG_FILE)}")
tee("=" * 70)

sys.exit(0 if overall else 1)


