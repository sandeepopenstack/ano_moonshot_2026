import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()

import json
import logging
import warnings

logging.getLogger("google.cloud.monitoring_v3").setLevel(logging.CRITICAL)
logging.getLogger("google.api_core.bidi").setLevel(logging.CRITICAL)
logging.getLogger("opentelemetry").setLevel(logging.CRITICAL)
warnings.filterwarnings("ignore", message="Failed to export metrics to Cloud Monitoring")

os.environ["GOOGLE_CLOUD_DISABLE_METRICS"] = "true"
os.environ["SPANNER_ENABLE_METRICS"] = "false"
os.environ["OTEL_PYTHON_DISABLED"] = "true"

from shared.events import (
    make_failure_notification_event,
    make_rca_confirmed_event,
    make_execution_completed_event,
    EVENT_BUS_KEY, NETWORK_STATUS_KEY,
    latest_key, publish_event,
    EVT_REFLEX_TRIAGE_READY,
    EVT_ENGINEER_READY,
    EVT_EXECUTION_COMPLETED,
)
from failure_injection_ms import build_trigger_event
from providers.detective_provider import generate_detective_output
from providers.execution_provider import generate_execution_output
from reflex_agent.tools import call_gnn_engine, perform_triage, publish_triage
from engineer_agent.tools import generate_healing_plan
from reflection_agent.tools import check_execution_result, evaluate_and_publish


class FakeCtx:
    def __init__(self, state):
        self.state = state


def separator(title):
    print("\n" + "=" * 65)
    print(f"  {title}")
    print("=" * 65)


USE_CASE_ID = os.environ.get("USE_CASE_ID", "uc1")

trigger_payload = build_trigger_event(use_case_id=USE_CASE_ID)

print("\n")
separator("STEP 2b: FailureInjectionMS \u2192 ReflexAgent")
print(f"  Source      : {trigger_payload['sourceSystem']}")
print(f"  Event Type  : {trigger_payload['eventType']}")
print(f"  Trigger     : {trigger_payload['trigger']}")
print(f"  Use Case ID : {trigger_payload['useCaseId']}")
print(f"  Domain      : {trigger_payload['domain']}")
print(f"  Affected    : {trigger_payload['affected_enodebs'] + trigger_payload['affected_core_elements']}")

failure_event = make_failure_notification_event(trigger_payload)
state = {
    NETWORK_STATUS_KEY:                      "ANOMALY_DETECTED",
    EVENT_BUS_KEY:                           [failure_event],
    latest_key(failure_event["event_type"]): failure_event,
}
ctx = FakeCtx(state)

print("\n" + "=" * 65)
print("  PIPELINE TEST \u2014 No LLM \u2014 Instant")
print("=" * 65)
print(f"  Trigger : {trigger_payload['sourceSystem']} \u2192 {trigger_payload['eventType']}")
print(f"  Use Case: {trigger_payload['useCaseId']} \u2014 {trigger_payload.get('label', '')}")
print(f"  Domain  : {trigger_payload['domain']}")
print(f"  Note    : Zero API calls \u2014 pure Python logic test")


separator("STEP 1: ReflexAgent (call_gnn_engine \u2192 perform_triage \u2192 publish_triage)")

r1 = call_gnn_engine(ctx)
print(f"\n  call_gnn_engine  \u2192 {r1}")

r2 = perform_triage(ctx)
print(f"  perform_triage   \u2192 {r2}")

r3 = publish_triage(ctx)
print(f"  publish_triage   \u2192 {r3}")

reflex_event   = state.get(latest_key(EVT_REFLEX_TRIAGE_READY), {})
reflex_payload = reflex_event.get("payload", {})

print(f"\n   ReflexAgent output:")
print(f"     domain_triage  : {reflex_payload.get('domain_triage')}")
print(f"     priority       : {reflex_payload.get('priority_external')}")
print(f"     entity_ids     : {reflex_payload.get('entity_ids')}")
print(f"     composite_score: {reflex_payload.get('composite_score')}")

expected_domain = trigger_payload.get("domain", "UNKNOWN")
assert reflex_payload.get("domain_triage", "").startswith(expected_domain), \
    f"FAIL: domain_triage should start with {expected_domain}, got {reflex_payload.get('domain_triage')}"
assert reflex_payload.get("priority_external") == "CRITICAL", \
    f"FAIL: priority should be CRITICAL, got {reflex_payload.get('priority_external')}"
assert len(reflex_payload.get("entity_ids", [])) >= 1, \
    f"FAIL: entity_ids should not be empty, got {reflex_payload.get('entity_ids')}"
print(f"\n   All ReflexAgent assertions PASSED")


separator("STEP 2: DetectiveAgent (mock)")

rca_output = generate_detective_output(reflex_payload)
rca_event  = make_rca_confirmed_event(
    source_event_id=reflex_event["event_id"],
    rca_output=rca_output,
)
publish_event(state, rca_event)

print(f"  root_cause       : {rca_output.get('root_cause')}")
print(f"  domain           : {rca_output.get('domain')}")
print(f"  confidence       : {rca_output.get('confidence_score')}")
print(f"  affected_entities: {rca_output.get('affected_entities')}")
print(f"  kpi_delta        : {rca_output.get('kpi_delta_pct')}%")
print(f"\n  DetectiveAgent mock published: {rca_event['event_type']}")


separator("STEP 3: EngineerAgent (generate_healing_plan)")

r4 = generate_healing_plan(ctx)
print(f"\n  status           : {r4.get('status')}")

engineer_event   = state.get(latest_key(EVT_ENGINEER_READY), {})
engineer_payload = engineer_event.get("payload", {})
tmf921           = engineer_payload.get("tmf921_intent", {})

print(f"\n   EngineerAgent output:")
print(f"     root_cause      : {engineer_payload.get('root_cause')}")
print(f"     priority        : {engineer_payload.get('priority')}")
print(f"     execution_order : {engineer_payload.get('execution_order')}")
print(f"     utility_score   : {engineer_payload.get('utility_scoring', {}).get('top_utility_score')}")
print(f"     intent_id       : {tmf921.get('intent_id')}")
print(f"     action_command  : {tmf921.get('primary_action_command', {}).get('type')}")
print(f"     recovery_targets: {[t.get('target_metric') for t in tmf921.get('expressions', [])]}")

assert r4.get("status") == "EVENT_PUBLISHED", \
    f"FAIL: status should be EVENT_PUBLISHED, got {r4.get('status')}"
assert engineer_payload.get("root_cause"), "FAIL: root_cause should not be empty"
assert engineer_payload.get("execution_order"), "FAIL: execution_order should not be empty"
print(f"\n   All EngineerAgent assertions PASSED")


separator("STEP 4: ExecutorAgent (mock)")

exec_output = generate_execution_output(engineer_payload)
exec_event  = make_execution_completed_event(
    source_event_id=engineer_event["event_id"],
    execution_output=exec_output,
)
publish_event(state, exec_event)

print(f"  success          : {exec_output.get('success')}")
print(f"  state            : {exec_output.get('state')}")
print(f"  activation_id    : {exec_output.get('activation_id')}")
print(f"  intent_id        : {exec_output.get('intent_id')}")
print(f"\n   ExecutorAgent mock published: {exec_event['event_type']}")


separator("STEP 5: ReflectionAgent (check_execution_result \u2192 evaluate_and_publish)")

r5 = check_execution_result(ctx)
print(f"\n  check_execution_result \u2192 {r5}")

r6 = evaluate_and_publish(ctx)
print(f"  evaluate_and_publish   \u2192 {r6}")

reflection_output = state.get("reflection_output", {})

print(f"\n  ReflectionAgent output:")
print(f"     status          : {reflection_output.get('status')}")
print(f"     resolved        : {reflection_output.get('resolved')}")
print(f"     gui_status      : {reflection_output.get('gui_status')}")
print(f"     gnn_topology    : {reflection_output.get('gnn_topology_view')}")
print(f"     business_view   : {reflection_output.get('business_view')}")
print(f"     service_view    : {reflection_output.get('service_view')}")
print(f"     zscore          : {reflection_output.get('zscore_comparison')}")

assert reflection_output.get("resolved") == True, \
    f"FAIL: should be resolved=True, got {reflection_output.get('resolved')}"
assert reflection_output.get("status") == "IMO_COMPLIES", \
    f"FAIL: status should be IMO_COMPLIES, got {reflection_output.get('status')}"
print(f"\n   All ReflectionAgent assertions PASSED")


separator("FINAL SUMMARY")

event_sequence = [e["event_type"] for e in state.get(EVENT_BUS_KEY, [])]

final_summary = {
    "network_status":    state.get(NETWORK_STATUS_KEY),
    "resolved":          reflection_output.get("resolved"),
    "reflection_status": reflection_output.get("status"),
    "gui_status":        reflection_output.get("gui_status"),
    "gnn_topology":      reflection_output.get("gnn_topology_view"),
    "business_view":     reflection_output.get("business_view"),
    "service_view":      reflection_output.get("service_view"),
    "zscore_validation": reflection_output.get("zscore_comparison"),
    "event_summary": {
        "total_events":   len(event_sequence),
        "event_sequence": event_sequence,
    },
    "timestamp": reflection_output.get("timestamp"),
}

print("  API RESPONSE")
print("  HTTP 200 OK\n")
print("  RESPONSE PAYLOAD")
print(json.dumps(final_summary, indent=4))

print("\n" + "=" * 65)
print("   ALL TESTS PASSED")
print("=" * 65)
