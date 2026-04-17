"""
Monitoring Agent Tools — Stage 5
==================================
Subscribes to:  gnn.anomaly.detected
Publishes:      monitoring.triage.ready

Event-driven: reads triggering GNN event from session state,
publishes triage event so InvestigationAgent reacts next.
"""

from google.adk.tools import ToolContext
import json

from app.events import (
    EVT_GNN_ANOMALY_DETECTED,
    consume_latest,
    publish_event,
    make_monitoring_triage_event,
    NETWORK_STATUS_KEY
)


def _infer_domain(nodes: list[str]) -> str:
    has_ran       = any("RAN"       in n for n in nodes)
    has_core      = any("HSS" in n or "CORE" in n for n in nodes)
    has_transport = any("TRANSPORT" in n for n in nodes)

    if sum([has_ran, has_core, has_transport]) > 1:
        return "CROSS_DOMAIN"
    if has_ran:       return "RAN"
    if has_core:      return "CORE"
    return "TRANSPORT"


def monitor_and_triage(tool_context: ToolContext) -> dict:
    """
    Stage 5 — Monitoring & Detection Agent tool.

    TRUE EVENT-DRIVEN BEHAVIOR:
      - ONLY reacts if gnn.anomaly.detected exists
      - NEVER pulls data on its own
      - Publishes monitoring.triage.ready
    """

    state = tool_context.state

    # ── Subscribe to GNN event ─────────────────────────────────────────────
    gnn_event_wrapper = consume_latest(state, EVT_GNN_ANOMALY_DETECTED)

    if not gnn_event_wrapper:
        # TRUE A2A: do nothing if no triggering event
        return {
            "status": "IDLE",
            "reason": "No gnn.anomaly.detected event available"
        }

    # ── Idempotency check (avoid re-processing same event) ────────────────
    last_processed = state.get("monitoring_last_event_id")

    if last_processed == gnn_event_wrapper["event_id"]:
        return {
            "status": "SKIPPED",
            "reason": "Event already processed",
            "event_id": last_processed
        }

    gnn_inference = gnn_event_wrapper["payload"]

    #  PRINT INPUT (GNN EVENT)
    print("\n================ MonitoringAgent =================")
    print("Received GNN Event:")
    print(json.dumps(gnn_inference, indent=2))

    nodes   = gnn_inference["anomalousSubgraph"]["nodes"]
    z_score = gnn_inference["anomalyScore"]["zScore"]
    domain  = _infer_domain(nodes)

    # ── Priority logic  ──────────────────────────────────────
    if z_score >= 8:
        priority_flag = "P1"
    elif z_score >= 5:
        priority_flag = "P2"
    else:
        priority_flag = "P3"

    ranked_branches = gnn_inference["rankedRemediationBranches"]
    execution_order = [b["action_id"] for b in ranked_branches]

    source_id = gnn_event_wrapper["event_id"]

    # ── Publish event (Stage 5 → 6) ───────────────────────────────────────
    event = make_monitoring_triage_event(
        source_event_id   = source_id,
        domain_triage     = domain,
        priority_flag     = priority_flag,
        subgraph          = gnn_inference["anomalousSubgraph"],
        confidence        = gnn_inference["anomalyScore"]["confidence"],
        business_priority = gnn_inference["businessPriority"],
        ranked_branches   = ranked_branches,
        execution_order   = execution_order,
    )

     # PRINT OUTPUT (WHAT MONITORING PRODUCED)
    print("\n🔹 Monitoring Output Event:")
    print(json.dumps(event, indent=2))

    publish_event(state, event)

    # ── Persist state updates ─────────────────────────────────────────────
    state["monitoring_last_event_id"] = gnn_event_wrapper["event_id"]
    state["monitoring_output"] = event
    state[NETWORK_STATUS_KEY] = "HEALING"

    return {
        "status": "EVENT_PUBLISHED",
        "published_event": event["event_type"],
        "event_id": event["event_id"],
        "domain_triage": domain,
        "priority_flag": priority_flag,
        "execution_order": execution_order,
        "next_agent": "InvestigationAgent",
        "network_status": state[NETWORK_STATUS_KEY],
    }