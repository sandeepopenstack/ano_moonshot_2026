"""
main.py — ReflectionAgent Standalone Entry Point
==================================================
Standalone test runner for ReflectionAgent only.

Starts directly from:
  STEP 9 — execution.completed event (ExecutorAgent output)

Flow:
  9   ExecutorAgent output → ReflectionAgent (this file seeds the pipeline)
  10  ReflectionAgent:
        check_execution_result → parse TMF641/TMF921
        evaluate_and_publish   → GNN post-action z-score + KPI validation
        → IMO_COMPLIES (RESOLVED) or RETRIGGER_INVESTIGATION

Use this to test ReflectionAgent in isolation — without running the full
5-loop pipeline. Feed it any execution.completed payload and see the
IMO_COMPLIES / RETRIGGER decision, z-score comparison, KPI validation.

Use case selection:
  USE_CASE_ID=uc1 python main.py   ← antenna tilt (default)
  USE_CASE_ID=uc2 python main.py   ← HSS failover
"""

import asyncio
import json
import logging
import os
import sys

# ── Import path fix ───────────────────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault("AGENT_MODEL", "gemini-2.5-flash")

# ── ADK imports ───────────────────────────────────────────────────────────────
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

# ── Project imports ───────────────────────────────────────────────────────────
from app.orchestrator.root_agent import root_agent
from app.events import (
    NETWORK_STATUS_KEY,
    EVENT_BUS_KEY,
    EVT_EXECUTION_COMPLETED,
    latest_key,
)
from app.workflow_state import extract_final_summary

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)

# ── Constants ─────────────────────────────────────────────────────────────────
APP_NAME    = "reflection_agent_app"
USER_ID     = "local_user"
MAX_LOOPS   = int(os.environ.get("MAX_PIPELINE_LOOPS", 4))
USE_CASE_ID = os.environ.get("USE_CASE_ID", "uc1")

# ── Step 9 input payloads — ExecutorAgent output for each UC ─────────────────
# These are what the ExecutorAgent (Ericsson EIC) sends back after execution.
# execution_provider.py generates these from the EngineerAgent TMF921 intent.

EXECUTOR_OUTPUTS = {

    # ─────────────────────────────────────────────────────────────────────────
    # UC1 — Antenna Tilt Misconfiguration (RAN)
    # Expected resolution: IMO_COMPLIES
    # pre_action_z_score: 9.4 (set by ReflexAgent call_gnn_engine)
    # Gate 2 and Gate 3 are read from Spanner/MCP in service mode.
    # KPI: dl_throughput 8.0→50.0 (post=49.5 ✓), rrc 60.0→99.5 (post=99.1 ✓)
    # ─────────────────────────────────────────────────────────────────────────
    "uc1": {
        "event_id":   "evt-exec-uc1-001",
        "event_type": EVT_EXECUTION_COMPLETED,
        "source":     "ExecutorAgent",
        "payload": {
            "eventId":       "EV-dekfn_efjnf_fefe",
            "success":       True,
            "state":         "completed",
            "error":         "",
            "activation_id": "ACT-SYN-002",
            "intent_id":     "INT-UC1-001",
            "timestamp":     "2026-05-10T13:00:06.000000+00:00",
            "order_type":    "ServiceOrder",
            "description":   "RAN_PARAM_ROLLBACK on 6 RAN entities — root_cause: antenna_tilt_misconfiguration",
            "tmf641_order": {
                "@type":       "ServiceOrder",
                "external_id": "ACT-SYN-002",
                "category":    "autonomous_healing",
                "priority":    "1",
                "state":       "acknowledged",
                "intent_id":   "INT-UC1-001",
                "order_items": [
                    {
                        "id":     "1",
                        "action": "modify",
                        "service": {
                            "id":   "RAN_PARAM_ROLLBACK",
                            "name": "RAN Healing — RAN_PARAM_ROLLBACK",
                            "service_characteristics": [
                                {"name": "action_type",           "value": "RAN_PARAM_ROLLBACK"},
                                {"name": "target_entities",       "value": "eNB-SYN-003,eNB-SYN-004,eNB-SYN-005,eNB-SYN-006,eNB-SYN-007,gNB-SYN-003"},
                                {"name": "parameter_name",        "value": "RollbackTiltParameters"},
                                {"name": "risk_level",            "value": "LOW"},
                                {"name": "reversible",            "value": "true"},
                                {"name": "estimated_ttr_minutes", "value": "130"},
                                {"name": "domain",                "value": "RAN"},
                            ],
                        },
                    }
                ],
                "related_parties": [
                    {"role": "requester",     "name": "OriginIDAgent"},
                    {"role": "executor",      "name": "Ericsson_EIC"},
                    {"role": "investigation", "name": "DetectiveAgent"},
                    {"role": "planning",      "name": "EngineerAgent"},
                ],
                "notes": [
                    {"author": "DetectiveAgent", "text": "RCD confirmed: antenna_tilt_misconfiguration"},
                    {"author": "EngineerAgent",  "text": "TMF921 Intent: Remediate antenna_tilt_misconfiguration"},
                    {"author": "ExecutorAgent",  "text": "Executing RAN_PARAM_ROLLBACK"},
                ],
            },
            "tmf921_intent": {
                "intent_id":     "INT-UC1-001",
                "intent_type":   "remediation",
                "root_cause":    "antenna_tilt_misconfiguration",
                "domain":        "RAN",
                "priority":      "HIGH",
                "target_entities": [
                    "eNB-SYN-003", "eNB-SYN-004", "eNB-SYN-005",
                    "eNB-SYN-006", "eNB-SYN-007", "gNB-SYN-003",
                ],
                "expressions": [
                    {
                        "target_metric": "dl_throughput_mbps",
                        "target_value":  50.0,
                        "current_value": 8.0,
                        "tolerance_pct": 10.0,
                    },
                    {
                        "target_metric": "rrc_setup_success_rate",
                        "target_value":  99.5,
                        "current_value": 60.0,
                        "tolerance_pct": 5.0,
                    },
                ],
                "constraints": {
                    "max_impact_duration_minutes": 130,
                    "reversible_required":         True,
                    "maintenance_window":          "immediate",
                },
            },
            "response": {
                "id":    "SO-ACT-SYN-002",
                "state": "acknowledged",
            },
        },
    },

    # ─────────────────────────────────────────────────────────────────────────
    # UC2 — HSS Failover (CORE)
    # Expected resolution: IMO_COMPLIES
    # pre_action_z_score: 9.4
    # KPI: attach_success_rate 72.0→99.8 (post=99.2 ✓), cpu 92.0→35.0 (post=38.0 ✓)
    # ─────────────────────────────────────────────────────────────────────────
    "uc2": {
        "event_id":   "evt-exec-uc2-001",
        "event_type": EVT_EXECUTION_COMPLETED,
        "source":     "ExecutorAgent",
        "payload": {
            "eventId":       "EV-dekfn_efjnf_gege",
            "success":       True,
            "state":         "completed",
            "error":         "",
            "activation_id": "ACT-SYN-001",
            "intent_id":     "INT-UC2-001",
            "timestamp":     "2026-05-10T13:00:06.000000+00:00",
            "order_type":    "ServiceOrder",
            "description":   "MZ_SESSION_CLEAR on 1 CORE entity — root_cause: hss_subscriber_db_saturation",
            "tmf641_order": {
                "@type":       "ServiceOrder",
                "external_id": "ACT-SYN-001",
                "category":    "autonomous_healing",
                "priority":    "1",
                "state":       "acknowledged",
                "intent_id":   "INT-UC2-001",
                "order_items": [
                    {
                        "id":     "1",
                        "action": "modify",
                        "service": {
                            "id":   "MZ_SESSION_CLEAR",
                            "name": "CORE Healing — MZ_SESSION_CLEAR",
                            "service_characteristics": [
                                {"name": "action_type",           "value": "MZ_SESSION_CLEAR"},
                                {"name": "target_entities",       "value": "HSS-SYN-01"},
                                {"name": "parameter_name",        "value": "ClearStaleSessions"},
                                {"name": "risk_level",            "value": "MEDIUM"},
                                {"name": "reversible",            "value": "true"},
                                {"name": "estimated_ttr_minutes", "value": "80"},
                                {"name": "domain",                "value": "CORE"},
                            ],
                        },
                    }
                ],
                "related_parties": [
                    {"role": "requester",     "name": "OriginIDAgent"},
                    {"role": "executor",      "name": "Ericsson_EIC"},
                    {"role": "investigation", "name": "DetectiveAgent"},
                    {"role": "planning",      "name": "EngineerAgent"},
                ],
                "notes": [
                    {"author": "DetectiveAgent", "text": "RCD confirmed: hss_subscriber_db_saturation"},
                    {"author": "EngineerAgent",  "text": "TMF921 Intent: Remediate hss_subscriber_db_saturation"},
                    {"author": "ExecutorAgent",  "text": "Executing MZ_SESSION_CLEAR"},
                ],
            },
            "tmf921_intent": {
                "intent_id":       "INT-UC2-001",
                "intent_type":     "remediation",
                "root_cause":      "hss_subscriber_db_saturation",
                "domain":          "CORE",
                "priority":        "MEDIUM",
                "target_entities": ["HSS-SYN-01"],
                "expressions": [
                    {
                        "target_metric": "attach_success_rate",
                        "target_value":  99.8,
                        "current_value": 72.0,
                        "tolerance_pct": 5.0,
                    },
                    {
                        "target_metric": "cpu_utilization_pct",
                        "target_value":  35.0,
                        "current_value": 92.0,
                        "tolerance_pct": 10.0,
                    },
                ],
                "constraints": {
                    "max_impact_duration_minutes": 80,
                    "reversible_required":         True,
                    "maintenance_window":          "immediate",
                },
            },
            "response": {
                "id":    "SO-ACT-SYN-001",
                "state": "acknowledged",
            },
        },
    },
}

# ── Validate use case ─────────────────────────────────────────────────────────
if USE_CASE_ID not in EXECUTOR_OUTPUTS:
    raise ValueError(f"Unsupported USE_CASE_ID={USE_CASE_ID}. Choose: {list(EXECUTOR_OUTPUTS)}")


# ── Pipeline runner ───────────────────────────────────────────────────────────

async def run_reflection() -> None:

    print("=" * 70)
    print("        REFLECTION AGENT — STANDALONE TEST")
    print("=" * 70)
    print(f"Model       : {os.environ.get('AGENT_MODEL')}")
    print(f"Use Case ID : {USE_CASE_ID}")
    print(f"Backend     : {'Vertex AI (GCP)' if os.environ.get('GOOGLE_GENAI_USE_VERTEXAI','').lower()=='true' else 'Gemini API'}")
    print("=" * 70)

    # ── Step 9 input: ExecutorAgent output ───────────────────────────────────
    executor_output = EXECUTOR_OUTPUTS[USE_CASE_ID]
    exec_payload    = executor_output["payload"]

    print("\n" + "=" * 70)
    print("[STEP 9] ExecutorAgent output → ReflectionAgent")
    print("=" * 70)
    print("\nAPI CALL")
    print("POST /reflection_event")
    print("\nEXECUTION PAYLOAD")
    print(json.dumps({
        "eventId":       exec_payload.get("eventId"),
        "success":       exec_payload.get("success"),
        "state":         exec_payload.get("state"),
        "activation_id": exec_payload.get("activation_id"),
        "intent_id":     exec_payload.get("intent_id"),
        "domain":        exec_payload.get("tmf921_intent", {}).get("domain"),
        "target_entities": exec_payload.get("tmf921_intent", {}).get("target_entities", []),
        "expressions":   exec_payload.get("tmf921_intent", {}).get("expressions", []),
    }, indent=2))

    # ── Seed session state with execution.completed ───────────────────────────
    # check_execution_result reads: consume_latest(state, EVT_EXECUTION_COMPLETED)
    # Seeds both EVENT_BUS_KEY and latest_key.
    # pre_action_z_score: 9.4 (set by ReflexAgent in full pipeline).
    # In standalone mode we inject it directly.
    initial_state = {
        NETWORK_STATUS_KEY:                    "HEALING",
        EVENT_BUS_KEY:                         [executor_output],
        latest_key(EVT_EXECUTION_COMPLETED):   executor_output,
        "pre_action_z_score":                  9.4,   # ReflexAgent composite_score mock
    }

    # ── ADK session ───────────────────────────────────────────────────────────
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        state=initial_state,
    )

    # ── ADK Runner (root_agent routes to ReflectionAgent) ────────────────────
    runner = Runner(
        app_name=APP_NAME,
        agent=root_agent,
        session_service=session_service,
    )

    trigger = types.Content(
        role="user",
        parts=[types.Part(text="process")],
    )

    print("\n[ReflectionAgent] Validating post-remediation state...\n")
    print("-" * 70)

    # ── Pipeline loop ─────────────────────────────────────────────────────────
    for loop_num in range(MAX_LOOPS):

        print(f"\n{'-' * 70}")
        print(f"PIPELINE LOOP {loop_num + 1}")
        print("-" * 70)

        try:
            async for _ in runner.run_async(
                session_id=session.id,
                user_id=USER_ID,
                new_message=trigger,
            ):
                pass

        except Exception as e:
            error_str = str(e)
            print(f"\n[PIPELINE ERROR] {error_str}")
            if any(code in error_str for code in ["429", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE"]):
                print("\n[RateLimit] Waiting 60s before retry...")
                await asyncio.sleep(60)
                continue
            raise

        session = await session_service.get_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=session.id,
        )

        network_status     = session.state.get(NETWORK_STATUS_KEY, "UNKNOWN")
        reflection_output  = session.state.get("reflection_output", {})

        if reflection_output:
            print(f"\n[Loop {loop_num + 1}] ReflectionAgent: reflection.result published")
            if network_status in ("RESOLVED", "FAILED", "ANOMALY_DETECTED"):
                print(f"[Loop {loop_num + 1}] Network Status: {network_status}")
            break

    # ── Final output ──────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)

    reflection_output = session.state.get("reflection_output", {})

    if reflection_output:
        print(f"\n  Status          : {reflection_output.get('status')}")
        print(f"  Resolved        : {reflection_output.get('resolved')}")
        print(f"  Execution OK    : {reflection_output.get('execution_ok')}")
        z = reflection_output.get("zscore_comparison", {})
        print(f"  Pre-Action Z    : {z.get('pre_action_z')}")
        print(f"  Baseline        : {z.get('baseline')}")
        print(f"  Z Resolved      : {z.get('z_resolved')}")
        print(f"\n  KPI Validation:")
        for kpi in reflection_output.get("kpi_validation", []):
            status = "✓" if kpi.get("within_tolerance") else "✗"
            print(f"    {status} {kpi.get('metric')}: post={kpi.get('post_value')} target={kpi.get('target')}")
        print(f"\n  GUI Dashboard:")
        print(f"    GUI Status     : {reflection_output.get('gui_status')}")
        print(f"    Business View  : {reflection_output.get('business_view')}")
        print(f"    Service View   : {reflection_output.get('service_view')}")
        print(f"    GNN Topology   : {reflection_output.get('gnn_topology_view')}")
        print(f"    Topology State : {reflection_output.get('topology_state')}")
        if not reflection_output.get("resolved"):
            print(f"\n  Retrigger Reason: {reflection_output.get('retrigger_reason')}")
        print()
    else:
        print("\n  [WARNING] No reflection_output found in state")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    asyncio.run(run_reflection())


if __name__ == "__main__":
    main()
