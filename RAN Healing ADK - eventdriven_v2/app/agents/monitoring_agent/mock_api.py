"""
app/agents/monitoring_agent/mock_api.py
=========================================
Stage 5 Mock API — Monitoring Agent.

Two responsibilities:
  FETCH : pull GNN inference event (Stage 4 → Stage 5)
  PUBLISH: push triage result downstream (Stage 5 → Stage 6)

In production replace:
  fetch_gnn_inference()       → Vertex AI GNN endpoint / Spanner graph service
  publish_monitoring_output() → InvestigationAgent A2A REST / Pub/Sub

Scenario names come from remediation_config — not hard-coded here.
"""

import random
import sys
import os

# ── Import path fix for Cloud Run ─────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from gnn_inference_provider import generate_gnn_inference_event 
from app.config.remediation_config import DOMAIN_TO_INVESTIGATION_SCENARIO  

# Scenarios available for autonomous injection (aligned with synth YAML scenarios)
_GNN_SCENARIOS = [
    "UC_MULTI_DOMAIN_HEALING",   # UC1 — RAN tilt
    "UC2_CORE_CONGESTION",       # UC2 — HSS saturation
    "UC3_TRANSPORT_FIBER_CUT",   # UC3 — backhaul fiber cut
]


def fetch_gnn_inference(scenario: str | None = None) -> dict:
    """
    Stage 4 → Stage 5.
    Fetch GNN anomaly inference payload.

    If scenario is None, autonomously picks UC1 / UC2 / UC3 at random —
    simulates unpredictable real-world anomaly injection.

    Returns raw GNN payload dict (NOT the wrapped event).
    Caller wraps with make_gnn_anomaly_event() before writing to session state.

    Production replacement:
        GET https://gnn-inference-service/v1/latest-anomaly
    """
    if scenario is None:
        scenario = random.choice(_GNN_SCENARIOS)
        print(f"[MonitoringAgent mock_api] GNN auto-selected scenario: {scenario}")

    return generate_gnn_inference_event(scenario=scenario)


def publish_monitoring_output(triage_payload: dict) -> dict:
    """
    Simulate publishing monitoring triage result to InvestigationAgent.

    In production this becomes an A2A REST call or Pub/Sub push to the
    Ericsson InvestigationAgent endpoint.

    Returns a receipt dict so tools.py can log the handoff.
    """
    domain = triage_payload.get("domain_triage", "UNKNOWN")
    investigation_scenario = DOMAIN_TO_INVESTIGATION_SCENARIO.get(domain, "UC_SINGLE_RAN")

    return {
        "status":                  "published",
        "target_agent":            "InvestigationAgent",
        "tmf_event_type":          "tmf.agent.monitoring.triageReady",
        "investigation_scenario":  investigation_scenario,
        "domain_routed_to":        domain,
        "payload":                 triage_payload,
    }
