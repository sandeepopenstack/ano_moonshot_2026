import os, sys, asyncio, json, logging
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

for noisy in ("google.cloud.monitoring_v3", "google.api_core.bidi", "opentelemetry", "LlmAgent", "AdkGoogleAIBaseModel"):
    logging.getLogger(noisy).setLevel(logging.CRITICAL)

from google.genai.types import Content, Part
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from ran_healing_shared.events import (
    make_failure_notification_event, make_rca_confirmed_event,
    make_execution_completed_event, EVENT_BUS_KEY, NETWORK_STATUS_KEY,
    latest_key, publish_event, EVT_REFLEX_TRIAGE_READY, EVT_ENGINEER_READY,
    EVT_EXECUTION_COMPLETED,
)
from ran_healing_shared.failure_injection_ms import build_trigger_event
from ran_healing_shared.providers.detective_provider import generate_detective_output
from ran_healing_shared.providers.execution_provider import generate_execution_output
from reflex_agent.agent import root_agent as reflex_agent
from engineer_agent.agent import root_agent as engineer_agent
from reflection_agent.agent import root_agent as reflection_agent

APP_NAME = "ran_healing_live"
USER_ID = "tester"
SESSION_ID = "live_001"

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
_RUN_TS = datetime.now().strftime("%Y%m%d_%H%M%S")
_LOG_FILE = os.path.join(LOG_DIR, f"live_test_{_RUN_TS}.log")
_SUMMARY_FILE = os.path.join(LOG_DIR, f"live_test_{_RUN_TS}_summary.json")
_LATEST_LOG = os.path.join(LOG_DIR, "latest.log")
_LATEST_SUMMARY = os.path.join(LOG_DIR, "latest_summary.json")

log = logging.getLogger("test_live")
log.setLevel(logging.INFO)
log.handlers.clear()
_fh = logging.FileHandler(_LOG_FILE, mode="w", encoding="utf-8")
_fh.setLevel(logging.INFO)
_fh.setFormatter(logging.Formatter("%(message)s"))
log.addHandler(_fh)
_sh = logging.StreamHandler(sys.stdout)
_sh.setLevel(logging.INFO)
_sh.setFormatter(logging.Formatter("%(message)s"))
log.addHandler(_sh)
for fh in (logging.FileHandler(_LATEST_LOG, mode="w", encoding="utf-8"),):
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(fh)


def separator(title):
    line = "\n" + "=" * 65
    log.info(line)
    log.info(f"  {title}")
    log.info("=" * 65)


def print_state(s, label=""):
    nb = s.get(NETWORK_STATUS_KEY, "?")
    k = latest_key(EVT_REFLEX_TRIAGE_READY)
    t = s.get(k, {})
    log.info(f"  [{label}] network_status={nb} | events={len(s.get(EVENT_BUS_KEY, []))} | reflex_triage={'yes' if t else 'no'}")


async def run_agent(agent, state, message="Start", agent_name="Agent"):
    ss = InMemorySessionService()
    runner = Runner(agent=agent, session_service=ss, app_name=APP_NAME)
    await ss.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID, state=dict(state))
    content = Content(role="user", parts=[Part(text=message)])

    log.info(f"\n  --- Running {agent_name} (LLM) ---")
    i = 0
    async for event in runner.run_async(user_id=USER_ID, session_id=SESSION_ID, new_message=content):
        if event.content and event.content.parts:
            for p in event.content.parts:
                if p.text and p.text.strip():
                    log.info(f"  [{agent_name}] {p.text[:160]}")
                if p.function_call:
                    log.info(f"  [{agent_name}] tool call: {p.function_call.name}({p.function_call.args if p.function_call.args else ''})")
                if p.function_response:
                    resp = str(p.function_response.response)[:120]
                    log.info(f"  [{agent_name}] tool response: {p.function_response.name} -> {resp}")
        i += 1

    sess = await ss.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID)
    new_state = dict(sess.state) if sess and sess.state else state
    new_state[EVENT_BUS_KEY] = state.get(EVENT_BUS_KEY, [])
    log.info(f"  --- {agent_name} done ({i} events) ---")
    return new_state


def _check_auth():
    has_gemini_key = bool(os.environ.get("GEMINI_API_KEY"))
    use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("true", "1")
    if not has_gemini_key and not use_vertex:
        log.info("""
  \u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557
  \u2551  NO LLM AUTHENTICATION FOUND                                \u2551
  \u2560\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2563
  \u2551  To run live agents you need either:                        \u2551
  \u2551                                                              \u2551
  \u2551  Option A: Gemini API Key (free)                             \u2551
  \u2551    export GEMINI_API_KEY=your-key                            \u2551
  \u2551                                                              \u2551
  \u2551  Option B: Vertex AI (GCP project)                           \u2551
  \u2551    export GOOGLE_GENAI_USE_VERTEXAI=true                     \u2551
  \u2551    export GOOGLE_CLOUD_PROJECT=your-project                  \u2551
  \u2551    gcloud auth application-default login                     \u2551
  \u2551                                                              \u2551
  \u2551  For now: use `python test.py` for no-LLM pipeline test     \u2551
  \u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d
""")
        return False
    return True


async def main():
    if not _check_auth():
        return 1

    USE_CASE_ID = os.environ.get("USE_CASE_ID", "uc1")
    trigger_payload = build_trigger_event(use_case_id=USE_CASE_ID)

    separator(f"PIPELINE: {trigger_payload['useCaseId']} - {trigger_payload.get('label', '')}")
    log.info(f"  Trigger    : {trigger_payload['trigger']}")
    log.info(f"  Domain     : {trigger_payload['domain']}")
    log.info(f"  Affected   : {trigger_payload['affected_enodebs'] + trigger_payload['affected_core_elements']}")
    log.info(f"  Agent Model: {os.environ.get('AGENT_MODEL', 'gemini-2.5-flash')}")
    log.info(f"  Vertex AI  : {os.environ.get('GOOGLE_GENAI_USE_VERTEXAI', 'not set')}")
    auth_type = "GEMINI_API_KEY" if os.environ.get("GEMINI_API_KEY") else "VERTEX_AI" if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI") else "NONE"
    log.info(f"  Auth       : {auth_type}")
    log.info(f"  Log file   : {_LOG_FILE}")
    log.info(f"  Summary    : {_SUMMARY_FILE}")

    failure_event = make_failure_notification_event(trigger_payload)
    state = {
        NETWORK_STATUS_KEY: "ANOMALY_DETECTED",
        EVENT_BUS_KEY: [failure_event],
        latest_key(failure_event["event_type"]): failure_event,
    }

    separator("STEP 1: ReflexAgent (LLM - 3 tools: call_gnn_engine -> perform_triage -> publish_triage)")
    try:
        state = await run_agent(reflex_agent, state, agent_name="ReflexAgent")
    except Exception as e:
        log.info(f"  [ReflexAgent] FAILED: {e}")
        return 1
    print_state(state, "post-reflex")

    reflex_event = state.get(latest_key(EVT_REFLEX_TRIAGE_READY), {})
    reflex_payload = reflex_event.get("payload", {})
    domain_triage = reflex_payload.get("domain_triage", "?")
    priority = reflex_payload.get("priority_external", "?")
    entity_count = len(reflex_payload.get("entity_ids", []))
    log.info(f"  domain_triage={domain_triage} | priority={priority} | entities={entity_count}")
    log.info(f"  ReflexAgent {'PASSED' if reflex_payload else 'FAILED'}")

    separator("STEP 2: DetectiveAgent (mock)")
    rca_output = generate_detective_output(reflex_payload)
    rca_event = make_rca_confirmed_event(source_event_id=reflex_event.get("event_id", ""), rca_output=rca_output)
    publish_event(state, rca_event)
    log.info(f"  root_cause={rca_output.get('root_cause')} | domain={rca_output.get('domain')} | confidence={rca_output.get('confidence_score')}")

    separator("STEP 3: EngineerAgent (LLM - 1 tool: generate_healing_plan)")
    try:
        state = await run_agent(engineer_agent, state, agent_name="EngineerAgent")
    except Exception as e:
        log.info(f"  [EngineerAgent] FAILED: {e}")
        return 1
    print_state(state, "post-engineer")

    engineer_event = state.get(latest_key(EVT_ENGINEER_READY), {})
    engineer_payload = engineer_event.get("payload", {})
    plan_status = engineer_payload.get("root_cause", "?")
    branch_count = len(engineer_payload.get("execution_order", []))
    log.info(f"  root_cause={plan_status} | branches={branch_count}")
    log.info(f"  EngineerAgent {'PASSED' if engineer_payload else 'FAILED'}")

    separator("STEP 4: ExecutorAgent (mock)")
    exec_output = generate_execution_output(engineer_payload)
    exec_event = make_execution_completed_event(source_event_id=engineer_event.get("event_id", ""), execution_output=exec_output)
    publish_event(state, exec_event)
    log.info(f"  success={exec_output.get('success')} | state={exec_output.get('state')} | activation={exec_output.get('activation_id')}")

    separator("STEP 5: ReflectionAgent (LLM - 2 tools: check_execution_result -> evaluate_and_publish)")
    try:
        state = await run_agent(reflection_agent, state, agent_name="ReflectionAgent")
    except Exception as e:
        log.info(f"  [ReflectionAgent] FAILED: {e}")
        return 1
    print_state(state, "post-reflection")

    reflection_output = state.get("reflection_output", {})
    resolved = reflection_output.get("resolved", False)
    status = reflection_output.get("status", "?")
    log.info(f"  resolved={resolved} | status={status} | gui={reflection_output.get('gui_status', '?')}")
    log.info(f"  ReflectionAgent {'PASSED' if reflection_output else 'FAILED'}")

    separator("SUMMARY")
    event_sequence = [e["event_type"] for e in state.get(EVENT_BUS_KEY, [])]
    passed = bool(reflex_payload) and bool(engineer_payload) and bool(reflection_output)
    summary = {
        "use_case": USE_CASE_ID,
        "trigger": trigger_payload["trigger"],
        "live_llm": True,
        "all_agents_passed": passed,
        "network_status": state.get(NETWORK_STATUS_KEY),
        "resolved": resolved,
        "reflection_status": status,
        "domain_triage": domain_triage,
        "event_sequence": event_sequence,
        "log_file": _LOG_FILE,
        "summary_file": _SUMMARY_FILE,
        "run_timestamp": _RUN_TS,
    }
    log.info(json.dumps(summary, indent=2))
    log.info(f"\n  {'ALL LIVE AGENTS PASSED' if passed else 'SOME AGENTS FAILED'}")

    with open(_SUMMARY_FILE, "w") as sf:
        json.dump(summary, sf, indent=2)
    with open(_LATEST_SUMMARY, "w") as sf:
        json.dump(summary, sf, indent=2)

    return 0 if passed else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
