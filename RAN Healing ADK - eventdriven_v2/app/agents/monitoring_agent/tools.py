"""
app/agents/monitoring_agent/tools.py
=======================================
Stage 5 — Monitoring & Detection Agent.

Flow:
  1. FETCH  — call mock_api.fetch_gnn_inference() to get GNN anomaly event
              (in production: Vertex AI GNN endpoint)
  2. TRIAGE — domain + priority from remediation_config (no hardcoding here)
  3. PUBLISH — call mock_api.publish_monitoring_output() to hand off downstream
              (in production: InvestigationAgent A2A API)
  4. EVENT  — write monitoring.triage.ready to session state event bus

All thresholds, domain patterns, priority values → remediation_config.py
All API calls (fetch / publish)                 → mock_api.py
"""

import json

from google.adk.tools import ToolContext

from app.events import (
    EVT_GNN_ANOMALY_DETECTED,
    NETWORK_STATUS_KEY,
    consume_latest,
    make_monitoring_triage_event,
    publish_event,
)
from app.config.remediation_config import infer_domain, get_priority_flag
from app.agents.monitoring_agent.mock_api import (
    fetch_gnn_inference,
    publish_monitoring_output,
)


def monitor_and_triage(tool_context: ToolContext) -> dict:
    """
    Stage 5 — Monitoring & Detection Agent tool.

    Event-driven:
      - Checks session state for gnn.anomaly.detected first (normal pipeline path)
      - If not present, FETCHES from GNN API via mock_api (bootstrap / retrigger path)
      - Publishes monitoring.triage.ready for InvestigationAgent
    """

    state = tool_context.state

    # ── Step 1: Get GNN event — from state OR fetch via API ───────────────
    # Normal path: GNN event was placed in state by main.py / ValidationAgent retrigger
    # Bootstrap path: state is empty, fetch directly from GNN inference provider
    gnn_wrapper = consume_latest(state, EVT_GNN_ANOMALY_DETECTED)

    if not gnn_wrapper:
        print("[MonitoringAgent] No GNN event in state — fetching from GNN API...")
        from app.events import make_gnn_anomaly_event
        gnn_payload = fetch_gnn_inference()          # ← mock_api call
        gnn_wrapper = make_gnn_anomaly_event(gnn_payload)
        publish_event(state, gnn_wrapper)             # write to event bus
        print(f"[MonitoringAgent] Fetched scenario: {gnn_payload.get('probableDomain')}")

    # ── Step 2: Idempotency — skip if already processed ──────────────────
    if state.get("monitoring_last_event_id") == gnn_wrapper["event_id"]:
        return {
            "status":   "SKIPPED",
            "reason":   "Event already processed",
            "event_id": gnn_wrapper["event_id"],
        }

    gnn = gnn_wrapper["payload"]

    print("\n================ MonitoringAgent — Stage 5 =================")
    print("INPUT — GNN Anomaly Event:")
    print(json.dumps(gnn, indent=2, default=str))

    # ── Step 3: Domain triage ─────────────────────────────────────────────
    # Uses remediation_config.infer_domain — aligned with topology.py naming
    nodes  = gnn["anomalousSubgraph"]["nodes"]
    domain = infer_domain(nodes)

    # ── Step 4: Priority flag ─────────────────────────────────────────────
    # Uses remediation_config.get_priority_flag — aligned with anomaly.py gates
    z_score       = gnn["anomalyScore"]["zScore"]
    priority_flag = get_priority_flag(z_score)

    if priority_flag == "NORMAL":
        print(f"[MonitoringAgent] z={z_score} below P3 threshold — no action needed")
        return {
            "status":    "BELOW_THRESHOLD",
            "z_score":   z_score,
            "threshold": "P3",
        }

    # ── Step 5: Sort branches by priority (highest first) ─────────────────
    ranked_branches = sorted(
        gnn["rankedRemediationBranches"],
        key=lambda b: b.get("priority_score", 0),
        reverse=True,
    )
    execution_order = [b["action_id"] for b in ranked_branches]

    # ── Step 6: Build triage payload ──────────────────────────────────────
    triage_payload = {
        "domain_triage":               domain,
        "priority_flag":               priority_flag,
        "z_score":                     z_score,
        "nodes":                       nodes,
        "subgraph":                    gnn["anomalousSubgraph"],
        "confidence":                  gnn["anomalyScore"]["confidence"],
        "business_priority":           gnn["businessPriority"],
        "ranked_remediation_branches": ranked_branches,
        "execution_order":             execution_order,
        "gnn_details":                 gnn.get("gnnDetails", {}),
    }

    # ── Step 7: Publish downstream via mock_api ───────────────────────────
    # This simulates the A2A handoff to InvestigationAgent
    handoff_receipt = publish_monitoring_output(triage_payload)   # ← mock_api call
    print(f"\n[MonitoringAgent] Downstream handoff: {handoff_receipt['target_agent']} "
          f"(scenario: {handoff_receipt.get('investigation_scenario')})")

    # ── Step 8: Write event to session state event bus ────────────────────
    event = make_monitoring_triage_event(
        source_event_id   = gnn_wrapper["event_id"],
        domain_triage     = domain,
        priority_flag     = priority_flag,
        subgraph          = gnn["anomalousSubgraph"],
        confidence        = gnn["anomalyScore"]["confidence"],
        business_priority = gnn["businessPriority"],
        ranked_branches   = ranked_branches,
        execution_order   = execution_order,
    )

    # Store gnn_details in event payload so InvestigationAgent can access tilt values
    event["payload"]["gnn_details"] = gnn.get("gnnDetails", {})

    print("\n[MonitoringAgent] OUTPUT — Triage Event:")
    print(json.dumps(event, indent=2, default=str))

    publish_event(state, event)

    state["monitoring_last_event_id"] = gnn_wrapper["event_id"]
    state["monitoring_output"]        = event
    state[NETWORK_STATUS_KEY]         = "HEALING"

    return {
        "status":           "EVENT_PUBLISHED",
        "published_event":  event["event_type"],
        "event_id":         event["event_id"],
        "domain_triage":    domain,
        "priority_flag":    priority_flag,
        "z_score":          z_score,
        "execution_order":  execution_order,
        "handoff_receipt":  handoff_receipt,
        "next_agent":       "InvestigationAgent",
        "network_status":   state[NETWORK_STATUS_KEY],
    }
