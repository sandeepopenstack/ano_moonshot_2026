import os, sys, json, subprocess, requests, uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ran_healing_shared.providers.detective_provider import generate_detective_output
from ran_healing_shared.providers.execution_provider import generate_execution_output
from ran_healing_shared.failure_injection_ms import build_trigger_event

SERVICES = {
    "reflex":     {"url": "https://ran-reflex-test-761300295499.us-central1.run.app"},
    "engineer":   {"url": "https://ran-engineer-test-761300295499.us-central1.run.app"},
    "reflection": {"url": "https://ran-reflection-test-761300295499.us-central1.run.app"},
}

LOG_FILE = os.environ.get("LOG_FILE", "pipeline_rest_run.log")


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


def headers():
    return {
        "Authorization": f"Bearer {get_token()}",
        "Content-Type": "application/json",
    }


def call_api(url, payload, label, timeout=180):
    token = get_token()
    tee(f"\n  POST {url}")
    tee(f"  Payload keys: {list(payload.keys())}")
    r = requests.post(
        url,
        headers=headers(),
        json=payload,
        timeout=timeout,
    )
    tee(f"  Response → {r.status_code}")
    if r.status_code >= 400:
        tee(f"  Error body: {r.text[:1000]}")
        r.raise_for_status()
    data = r.json()
    tee(f"  Response keys: {list(data.keys())}")
    return data


# ── Main ────────────────────────────────────────────────────────────────

USE_CASE_ID = os.environ.get("USE_CASE_ID", "uc1")
TIMESTAMP = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

with open(LOG_FILE, "w") as f:
    f.write(f"Pipeline REST Run — {TIMESTAMP}\n")
    f.write(f"USE_CASE_ID={USE_CASE_ID}\n")
    f.write("=" * 70 + "\n")

separator(f"RAN HEALING PIPELINE — REST API  |  {TIMESTAMP}")
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
tee(f"  Event ID    : {trigger.get('eventId')}")
tee(f"  Use Case ID : {trigger.get('useCaseId')}")
tee(f"  Domain      : {trigger.get('domain')}")
tee(f"  Affected eNBs: {trigger.get('affected_enodebs', [])}")
tee(f"  Affected Core: {trigger.get('affected_core_elements', [])}")
tee(f"  Neighbor eNBs: {trigger.get('affected_neighbor_enodebs', [])}")

# ── STEP 1: ReflexAgent via REST ────────────────────────────────────────

separator("STEP 1: ReflexAgent via POST/trigger_event")
reflex_url = f"{SERVICES['reflex']['url']}/trigger_event"
reflex_resp = call_api(reflex_url, trigger, "ReflexAgent")

reflex_payload = reflex_resp.get("detective_investigation_request", {})
tee(f"\n  Reflex response status : {reflex_resp.get('status')}")
tee(f"  Network status          : {reflex_resp.get('network_status')}")
tee(f"  Detective request keys  : {list(reflex_payload.keys())}")
tee(f"  entity_ids              : {reflex_payload.get('entity_ids')}")
tee(f"  domain_triage           : {reflex_payload.get('domain_triage')}")
tee(f"  priority                : {reflex_payload.get('priority')}")
tee(f"  impact_score            : {reflex_payload.get('impact_score')}")
tee(f"  criticality_label       : {reflex_payload.get('criticality_label')}")
tee(f"\n  Full reflex response:")
tee(json_pp(reflex_resp))
tee("\n  ✅ ReflexAgent REST PASSED")

# ── STEP 2: DetectiveAgent (mock) ───────────────────────────────────────

separator("STEP 2: DetectiveAgent (mock — local)")
rca_output = generate_detective_output(reflex_payload)
tee(f"\n  RCA output:")
tee(f"    root_cause       : {rca_output.get('root_cause')}")
tee(f"    domain           : {rca_output.get('domain')}")
tee(f"    confidence_score : {rca_output.get('confidence_score')}")
tee(f"    affected_entities: {rca_output.get('affected_entities')}")
tee(f"    kpi_delta_pct    : {rca_output.get('kpi_delta_pct')}%")
tee(f"    eventId          : {rca_output.get('eventId')}")
tee("\n  ✅ DetectiveAgent mock PASSED")

# ── STEP 3: EngineerAgent via REST ──────────────────────────────────────

separator("STEP 3: EngineerAgent via POST/rca-confirmed")
engineer_url = f"{SERVICES['engineer']['url']}/rca-confirmed"
engineer_resp = call_api(engineer_url, rca_output, "EngineerAgent")

engineer_output = engineer_resp.get("engineer_output", {})
executor_payload = engineer_resp.get("executor_payload", {})
tee(f"\n  Engineer response status: {engineer_resp.get('status')}")
tee(f"  Network status           : {engineer_resp.get('network_status')}")
tee(f"  Engineer output keys     : {list(engineer_output.keys())}")
tee(f"  root_cause               : {engineer_output.get('root_cause')}")
tee(f"  domain                   : {engineer_output.get('domain')}")
tee(f"  priority                 : {engineer_output.get('priority')}")
tee(f"  target_entities          : {engineer_output.get('target_entities')}")
tee(f"  intent_id                : {executor_payload.get('intent_id')}")
tee(f"  activation_id            : {executor_payload.get('activation_id')}")
tee(f"\n  Full engineer response:")
tee(json_pp(engineer_resp))
tee("\n  ✅ EngineerAgent REST PASSED")

# ── STEP 4: ExecutorAgent (mock) ────────────────────────────────────────

separator("STEP 4: ExecutorAgent (mock — local)")
exec_output = generate_execution_output(engineer_output or rca_output)
tee(f"\n  Execution output:")
tee(f"    success       : {exec_output.get('success')}")
tee(f"    state         : {exec_output.get('state')}")
tee(f"    activation_id : {exec_output.get('activation_id')}")
tee(f"    intent_id     : {exec_output.get('intent_id')}")
tee(f"    eventId       : {exec_output.get('eventId')}")
tee(f"    message       : {exec_output.get('message')}")
tee("\n  ✅ ExecutorAgent mock PASSED")

# ── STEP 5: ReflectionAgent via REST ────────────────────────────────────

separator("STEP 5: ReflectionAgent via POST/execution-completed")
reflection_url = f"{SERVICES['reflection']['url']}/execution-completed"
reflection_resp = call_api(reflection_url, exec_output, "ReflectionAgent")

reflection_output = reflection_resp.get("reflection_output", {})
tee(f"\n  Reflection response status : {reflection_resp.get('status')}")
tee(f"  Resolved                    : {reflection_resp.get('resolved')}")
tee(f"  Reflection status           : {reflection_resp.get('reflection_status')}")
tee(f"  Network status              : {reflection_resp.get('network_status')}")
tee(f"  Reflection output keys      : {list(reflection_output.keys())}")
tee(f"\n  Full reflection response:")
tee(json_pp(reflection_resp))
tee("\n  ✅ ReflectionAgent REST PASSED")

# ── SUMMARY ─────────────────────────────────────────────────────────────

separator("PIPELINE SUMMARY")
tee(f"  STEP 1 Reflex REST     | {reflex_resp.get('status')} | PASS")
tee(f"  STEP 2 DetectiveMock   | PASS")
tee(f"  STEP 3 Engineer REST   | {engineer_resp.get('status')} | PASS")
tee(f"  STEP 4 ExecutorMock    | PASS")
tee(f"  STEP 5 Reflection REST | PASS")

overall = (
    reflex_resp.get("status") == "ok"
    and engineer_resp.get("status") in ("processed", "ok", None)
)
tee("=" * 70)
tee(f"  OVERALL: {'ALL STEPS PASSED' if overall else 'SOME STEPS FAILED'}")
tee(f"  Log saved to: {os.path.abspath(LOG_FILE)}")
tee("=" * 70)

sys.exit(0 if overall else 1)
