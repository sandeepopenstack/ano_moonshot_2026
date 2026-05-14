"""
main.py — EngineerAgent Standalone Entry Point
================================================
Standalone test runner for EngineerAgent only.

Starts directly from:
  STEP 6 — detective.rca.confirmed event (Detective Agent output)

Flow:
  6   Detective Agent output → EngineerAgent (this file seeds the pipeline)
  7   EngineerAgent → generate_healing_plan
      → Utility scoring + branch ranking + TMF921 intent
  8   Output: engineer.ready → ExecutorAgent (displayed, not executed here)

Use this to test EngineerAgent in isolation — without running the full
5-loop pipeline from main.py. Feed it any detective.rca.confirmed payload
and see the healing plan, utility scores, and TMF921 intent.

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
    EVT_DETECTIVE_RCA_CONFIRMED,
    latest_key,
)
from app.workflow_state import extract_final_summary

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)

# ── Constants ─────────────────────────────────────────────────────────────────
APP_NAME  = "engineer_agent_app"
USER_ID   = "local_user"
MAX_LOOPS = int(os.environ.get("MAX_PIPELINE_LOOPS", 6))
USE_CASE_ID = os.environ.get("USE_CASE_ID", "uc1")

# ── Step 6 input payloads — Detective Agent output for each UC ────────────────
# These are what the Detective Agent (Ericsson) sends to EngineerAgent.
# via the detective_provider.py mock (or real Ericsson API when available).

DETECTIVE_OUTPUTS = {

    # ─────────────────────────────────────────────────────────────────────────
    # UC1 — Antenna Tilt Misconfiguration (RAN)
    # root_cause: antenna_tilt_misconfiguration
    # risk: 0.4 (ran_tilt_healing.yaml resolution.risk_score)
    # rev:  0.95 (ran_tilt_healing.yaml resolution.reversibility_score)
    # utility A: 0.94 × 1.0 × (1-0.4) × 0.95 = 0.5358 → Seq 1
    # utility B: 0.94 × 1.0 × (1-0.5) × 0.95 = 0.4465 → Seq 2
    # utility C: 0.94 × 1.0 × (1-0.8) × 0.95 = 0.1786 → Seq 3
    # ─────────────────────────────────────────────────────────────────────────
    "uc1": {
        "event_id":   "evt-det-uc1-001",
        "event_type": EVT_DETECTIVE_RCA_CONFIRMED,
        "source":     "DetectiveAgent",
        "payload": {
            "eventId":          "EV-dekfn_efjnf_fefe",
            "root_cause":       "antenna_tilt_misconfiguration",
            "domain":           "RAN",
            "confidence_score": 0.85,
            "confidence":       "HIGH",
            "hypothesis_id":    "RCH-UC1-001",
            "change_request_id": "CR-SYN-002",
            "incident_type":    "RAN_PARAMETER_PUSH",
            "root_cause_description": "RAN_PARAMETER_PUSH on eNB-SYN-003",
            "affected_entities": [
                "eNB-SYN-003", "eNB-SYN-004", "eNB-SYN-005",
                "eNB-SYN-006", "eNB-SYN-007", "gNB-SYN-003",
            ],
            "causal_parameters": {
                "parameter":      "antenna_tilt_degrees",
                "previous_value": 47.1,
                "current_value":  42.1,
                "unit":           "degrees",
                "change_source":  "change_request_CR-SYN-002",
            },
            "suggested_remediation": [
                {
                    "option":    "A",
                    "action":    "revert_antenna_tilt",
                    "target":    "eNB-SYN-003",
                    "param":     "antenna_tilt",
                    "value":     47.1,
                    "direction": "",
                    "note":      "Revert to pre-change state, target dl_throughput_mbps restored",
                },
                {
                    "option":    "B",
                    "action":    "adjust_neighbor_antenna_tilt",
                    "target":    "eNB-SYN-004",
                    "param":     "antenna_tilt",
                    "direction": "compensate",
                    "note":      "Adjust neighbor to absorb overflow from primary node",
                },
                {
                    "option": "C",
                    "action": "accept_degradation",
                    "target": "",
                    "note":   "Accept if RAN_PARAMETER_PUSH was intentional",
                },
            ],
            "recovery_targets": [
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
            "kpi_impact": {
                "primary_metric":           "dl_throughput_mbps",
                "primary_metric_delta_pct": -84.0,
                "secondary_impacts": {
                    "dl_prb_utilization_pct": 35.2,
                    "latency_ms":             22.4,
                    "handover_success_rate":  -12.1,
                },
            },
            "kpi_delta_pct":       84.0,
            "risk_score":          0.4,
            "reversibility_score": 0.95,
            "impact_score":        0.94,
            "criticality_score":   1.0,
            "criticality_label":   "CRITICAL",
            "alarm_ids":           ["ALM-001", "ALM-002", "ALM-003"],
            "affected_hex_bins":   ["87283472bffffff", "87283472affffff"],
            "primary_resource": {
                "node_id": "eNB-SYN-003",
                "sector":  1,
                "type":    "RAN",
            },
            "evidence_chain": [
                "GNN origin identification: eNB-SYN-003 ranked #1 in anomalous subgraph",
                "Z-score decomposition: z_ran=5.2, z_sev=4.1, z_vol=3.8 — RAN-domain confirmed",
                "Alarm correlation: 3 CATA alarms — RAN_PARAMETER_PUSH triggered",
                "Config change: CR-SYN-002 (RAN_PARAMETER_PUSH)",
                "KPI validation: dl_throughput_mbps changed -84.0% across 5 entities",
                "Topology trace: shared infrastructure — RAN domain confirmed",
                "Transport health: healthy",
                "RCD match: antenna_tilt_misconfiguration (confidence 0.85)",
            ],
        },
    },

    # ─────────────────────────────────────────────────────────────────────────
    # UC2 — HSS Failover (CORE)
    # root_cause: hss_subscriber_db_saturation
    # risk: 0.6 (core_congestion_healing.yaml resolution.risk_score)
    # rev:  0.7  (core_congestion_healing.yaml resolution.reversibility_score)
    # utility A: 0.94 × 1.0 × (1-0.6) × 0.70 = 0.2632 → Seq 1
    # utility B: 0.94 × 1.0 × (1-0.7) × 0.70 = 0.1974 → Seq 2
    # utility C: 0.94 × 1.0 × (1-1.0) × 0.70 = 0.0000 → Seq 3
    # ─────────────────────────────────────────────────────────────────────────
    "uc2": {
        "event_id":   "evt-det-uc2-001",
        "event_type": EVT_DETECTIVE_RCA_CONFIRMED,
        "source":     "DetectiveAgent",
        "payload": {
            "eventId":          "EV-dekfn_efjnf_gege",
            "root_cause":       "hss_subscriber_db_saturation",
            "domain":           "CORE",
            "confidence_score": 0.90,
            "confidence":       "HIGH",
            "hypothesis_id":    "RCH-UC2-001",
            "change_request_id": "CR-SYN-001",
            "incident_type":    "FAILOVER_MIGRATION",
            "root_cause_description": "HSS subscriber DB saturation on HSS-SYN-01",
            "affected_entities": ["HSS-SYN-01"],
            "causal_parameters": {
                "parameter":      "session_count",
                "previous_value": 45000,
                "current_value":  98000,
                "unit":           "sessions",
                "change_source":  "change_request_CR-SYN-001",
            },
            "suggested_remediation": [
                {
                    "option": "A",
                    "action": "clear_stale_hss_sessions",
                    "target": "HSS-SYN-01",
                    "note":   "Clear stale sessions to restore HSS capacity",
                },
                {
                    "option": "B",
                    "action": "shift_traffic_to_secondary_hss",
                    "target": "HSS-SYN-02",
                    "note":   "Failover subscribers to secondary HSS",
                },
                {
                    "option": "C",
                    "action": "accept_degradation",
                    "target": "",
                    "note":   "Accept if FAILOVER_MIGRATION was intentional",
                },
            ],
            "recovery_targets": [
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
            "kpi_impact": {
                "primary_metric":           "attach_success_rate",
                "primary_metric_delta_pct": -27.8,
                "secondary_impacts": {
                    "session_setup_failure_pct": 35.0,
                    "cpu_utilization_pct":       92.0,
                },
            },
            "kpi_delta_pct":       27.8,
            "risk_score":          0.6,
            "reversibility_score": 0.7,
            "impact_score":        0.94,
            "criticality_score":   1.0,
            "criticality_label":   "CRITICAL",
            "alarm_ids":           ["ALM-010", "ALM-011"],
            "affected_hex_bins":   [],
            "primary_resource": {
                "node_id": "HSS-SYN-01",
                "type":    "CORE",
            },
            "evidence_chain": [
                "GNN origin identification: HSS-SYN-01 ranked #1 in anomalous subgraph",
                "Z-score decomposition: z_core=6.1, z_sev=5.2, z_vol=4.8 — CORE-domain confirmed",
                "Alarm correlation: 2 CATA alarms — FAILOVER_MIGRATION triggered",
                "Config change: CR-SYN-001 (FAILOVER_MIGRATION)",
                "KPI validation: attach_success_rate changed -27.8%",
                "Topology trace: shared infrastructure — CORE domain confirmed",
                "Transport health: healthy",
                "RCD match: hss_subscriber_db_saturation (confidence 0.90)",
            ],
        },
    },
}

# ── Validate use case ─────────────────────────────────────────────────────────
if USE_CASE_ID not in DETECTIVE_OUTPUTS:
    raise ValueError(f"Unsupported USE_CASE_ID={USE_CASE_ID}. Choose: {list(DETECTIVE_OUTPUTS)}")


# ── Pipeline runner ───────────────────────────────────────────────────────────

async def run_engineer() -> None:

    print("=" * 70)
    print("           ENGINEER AGENT — STANDALONE TEST")
    print("=" * 70)
    print(f"Model       : {os.environ.get('AGENT_MODEL')}")
    print(f"Use Case ID : {USE_CASE_ID}")
    print(f"Backend     : {'Vertex AI (GCP)' if os.environ.get('GOOGLE_GENAI_USE_VERTEXAI','').lower()=='true' else 'Gemini API'}")
    print("=" * 70)

    # ── Step 6 input: Detective Agent output ──────────────────────────────────
    detective_output = DETECTIVE_OUTPUTS[USE_CASE_ID]
    detective_payload = detective_output["payload"]

    print("\n" + "=" * 70)
    print("[STEP 6] Detective Agent output → EngineerAgent")
    print("=" * 70)
    print("\nAPI CALL")
    print("POST /engineer_event")
    print("\nREQUEST PAYLOAD")
    print(json.dumps(detective_payload, indent=2))

    # ── Seed session state with detective.rca.confirmed ───────────────────────
    # generate_healing_plan reads: consume_latest(state, EVT_DETECTIVE_RCA_CONFIRMED)
    # Seeds both EVENT_BUS_KEY and latest_key so the tool finds it.
    initial_state = {
        NETWORK_STATUS_KEY:                          "HEALING",
        EVENT_BUS_KEY:                               [detective_output],
        latest_key(EVT_DETECTIVE_RCA_CONFIRMED):     detective_output,
    }

    # ── ADK session ───────────────────────────────────────────────────────────
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        state=initial_state,
    )

    # ── ADK Runner (full pipeline orchestrator) ───────────────────────────────
    # root_agent routes detective.rca.confirmed → EngineerAgent
    runner = Runner(
        app_name=APP_NAME,
        agent=root_agent,
        session_service=session_service,
    )

    trigger = types.Content(
        role="user",
        parts=[types.Part(text="process")],
    )

    print("\n[EngineerAgent] Generating healing plan...\n")
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

        network_status = session.state.get(NETWORK_STATUS_KEY, "UNKNOWN")

        # EngineerAgent sets HEALING — stop after it publishes engineer.ready
        engineer_output = session.state.get("engineer_output", {})
        if engineer_output:
            print(f"\n[Loop {loop_num + 1}] EngineerAgent: engineer.ready published")
            break

    # ── Final output ──────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)

    engineer_output = session.state.get("engineer_output", {})

    if engineer_output:
        print(f"\n  Intent ID        : {engineer_output.get('tmf921_intent', {}).get('intent_id')}")
        print(f"  Root Cause       : {engineer_output.get('root_cause')}")
        print(f"  Root Cause Mapped: {engineer_output.get('root_cause_mapped')}")
        print(f"  Domain           : {engineer_output.get('domain')}")
        print(f"  Priority         : {engineer_output.get('priority')}")
        print(f"  Branch Count     : {engineer_output.get('utility_scoring', {}).get('branch_count')}")
        print(f"  Top Utility Score: {engineer_output.get('utility_scoring', {}).get('top_utility_score')}")
        print(f"\n  Execution Order (sequence 1 = highest utility = Executor runs first):")
        for step in engineer_output.get("execution_order", []):
            print(f"    Seq {step['sequence']}: {step['action']} / {step['action_detail']} "
                  f"| utility={step['utility_score']}")
        print()
        print("  Full engineer_output:")
        print(json.dumps(engineer_output, indent=2, default=str))
    else:
        print("\n  [WARNING] No engineer_output found in state")
        print("  Final session state:")
        print(json.dumps(
            {k: v for k, v in session.state.items() if not k.startswith("_")},
            indent=2, default=str,
        ))


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    asyncio.run(run_engineer())


if __name__ == "__main__":
    main()
