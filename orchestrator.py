import os, json, requests, sys
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv()

from ran_healing_shared.failure_injection_ms import build_trigger_event
from ran_healing_shared.events import (
    make_failure_notification_event, make_rca_confirmed_event,
    make_execution_completed_event, EVENT_BUS_KEY, NETWORK_STATUS_KEY,
    latest_key, publish_event, EVT_REFLEX_TRIAGE_READY, EVT_ENGINEER_READY,
    EVT_EXECUTION_COMPLETED,
)
from ran_healing_shared.providers.detective_provider import generate_detective_output
from ran_healing_shared.providers.execution_provider import generate_execution_output

USER_ID = "orchestrator"
USE_CASE_ID = os.environ.get("USE_CASE_ID", "uc1")

# ADK base URL — single server serves all apps in local dev
ADK_BASE_URL = os.environ.get("ADK_BASE_URL", "http://localhost:8000").rstrip("/")

# Per-agent app names registered on the ADK server
AGENTS = {
    "reflex":     {"app": "reflex_agent",     "url": ADK_BASE_URL},
    "engineer":   {"app": "engineer_agent",   "url": ADK_BASE_URL},
    "reflection": {"app": "reflection_agent", "url": ADK_BASE_URL},
}

# Override URLs individually for Cloud Run (one agent per service)
for key in AGENTS:
    env_val = os.environ.get(f"{key.upper()}_AGENT_URL")
    if env_val:
        AGENTS[key]["url"] = env_val.rstrip("/")


def _create_session(base_url: str, app_name: str, state: dict) -> str:
    resp = requests.post(
        f"{base_url}/apps/{app_name}/users/{USER_ID}/sessions",
        json={"state": state},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _run_agent(base_url: str, app_name: str, session_id: str, message: str = "Process the event") -> list:
    body = {
        "appName": app_name,
        "userId": USER_ID,
        "sessionId": session_id,
        "newMessage": {"role": "user", "parts": [{"text": message}]},
    }
    resp = requests.post(
        f"{base_url}/run",
        json=body,
        timeout=120,
    )
    resp.raise_for_status()
    events = resp.json()

    for evt in events:
        parts = (evt.get("content") or {}).get("parts", []) if evt.get("content") else []
        for p in parts:
            if p.get("text"):
                print(f"  [{evt.get('author','agent')}] {p['text'][:200]}")
            if p.get("function_call"):
                print(f"  [{evt.get('author','agent')}] 󰄾 {p['function_call'].get('name')}")
            if p.get("function_response"):
                fr = p["function_response"]
                resp_text = str(fr.get("response", ""))[:120]
                print(f"  [{evt.get('author','agent')}] 󰇮 {fr.get('name')} -> {resp_text}")

    return events


def _get_state(base_url: str, app_name: str, session_id: str) -> dict:
    resp = requests.get(
        f"{base_url}/apps/{app_name}/users/{USER_ID}/sessions/{session_id}",
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("state", {})


def separator(title: str):
    print("\n" + "=" * 65)
    print(f"  {title}")
    print("=" * 65)


def main():
    trigger_payload = build_trigger_event(use_case_id=USE_CASE_ID)
    failure_event = make_failure_notification_event(trigger_payload)
    state = {
        NETWORK_STATUS_KEY: "ANOMALY_DETECTED",
        EVENT_BUS_KEY: [failure_event],
        latest_key(failure_event["event_type"]): failure_event,
    }

    separator(f"PIPELINE: {trigger_payload['useCaseId']} — {trigger_payload.get('label', '')}")
    print(f"  ADK Base      : {ADK_BASE_URL}")
    for name, cfg in AGENTS.items():
        print(f"  {name:>12} : {cfg['app']} @ {cfg['url']}")

    separator("STEP 1: ReflexAgent")
    cfg = AGENTS["reflex"]
    try:
        sid = _create_session(cfg["url"], cfg["app"], state)
        _run_agent(cfg["url"], cfg["app"], sid)
        state = _get_state(cfg["url"], cfg["app"], sid)
    except Exception as e:
        print(f"  [ERROR] ReflexAgent: {e}")
        return 1

    reflex_event = state.get(latest_key(EVT_REFLEX_TRIAGE_READY), {})
    reflex_payload = reflex_event.get("payload", {})
    print(f"  domain_triage={reflex_payload.get('domain_triage')} | priority={reflex_payload.get('priority_external')}")
    if not reflex_payload:
        print("  [ERROR] ReflexAgent did not produce reflex.triage.ready")
        return 1

    separator("STEP 2: DetectiveAgent (mock)")
    rca_output = generate_detective_output(reflex_payload)
    rca_event = make_rca_confirmed_event(
        source_event_id=reflex_event.get("event_id", ""),
        rca_output=rca_output,
    )
    publish_event(state, rca_event)
    print(f"  root_cause={rca_output.get('root_cause')} | domain={rca_output.get('domain')}")

    separator("STEP 3: EngineerAgent")
    cfg = AGENTS["engineer"]
    try:
        sid = _create_session(cfg["url"], cfg["app"], state)
        _run_agent(cfg["url"], cfg["app"], sid)
        state = _get_state(cfg["url"], cfg["app"], sid)
    except Exception as e:
        print(f"  [ERROR] EngineerAgent: {e}")
        return 1

    engineer_event = state.get(latest_key(EVT_ENGINEER_READY), {})
    engineer_payload = engineer_event.get("payload", {})
    print(f"  root_cause={engineer_payload.get('root_cause')} | branches={len(engineer_payload.get('execution_order', []))}")
    if not engineer_payload:
        print("  [ERROR] EngineerAgent did not produce engineer.ready")
        return 1

    separator("STEP 4: ExecutorAgent (mock)")
    exec_output = generate_execution_output(engineer_payload)
    exec_event = make_execution_completed_event(
        source_event_id=engineer_event.get("event_id", ""),
        execution_output=exec_output,
    )
    publish_event(state, exec_event)
    print(f"  success={exec_output.get('success')} | activation={exec_output.get('activation_id')}")

    separator("STEP 5: ReflectionAgent")
    cfg = AGENTS["reflection"]
    try:
        sid = _create_session(cfg["url"], cfg["app"], state)
        _run_agent(cfg["url"], cfg["app"], sid)
        state = _get_state(cfg["url"], cfg["app"], sid)
    except Exception as e:
        print(f"  [ERROR] ReflectionAgent: {e}")
        return 1

    reflection_output = state.get("reflection_output", {})
    resolved = reflection_output.get("resolved", False)
    print(f"  resolved={resolved} | status={reflection_output.get('status')}")

    separator("SUMMARY")
    final = {
        "use_case": USE_CASE_ID,
        "network_status": state.get(NETWORK_STATUS_KEY),
        "resolved": resolved,
        "reflection_status": reflection_output.get("status"),
        "event_sequence": [e["event_type"] for e in state.get(EVENT_BUS_KEY, [])],
        "pre_action_z": state.get("pre_action_z_score"),
    }
    print(json.dumps(final, indent=2))
    print(f"\n  {'ALL AGENTS PASSED' if resolved else 'PIPELINE FAILED'}")
    return 0 if resolved else 1


if __name__ == "__main__":
    sys.exit(main())
