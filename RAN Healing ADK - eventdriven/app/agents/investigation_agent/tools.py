from google.adk.tools import ToolContext
import json

from investigation_mock_output import (
    generate_investigation_output,
    publish_investigation_output,
)

from app.events import (
    EVT_MONITORING_TRIAGE_READY,
    consume_latest,
    publish_event,
    make_rca_confirmed_event,
)


def run_investigation_mock(tool_context: ToolContext) -> dict:
    """
    Stage 6 — Investigation Agent (mock).

    Subscribes to: monitoring.triage.ready
    Publishes:     investigation.rca.confirmed
    """

    state = tool_context.state

    # ── Subscribe to Monitoring event ─────────────────────────────────────
    triage_event = consume_latest(state, EVT_MONITORING_TRIAGE_READY)

    if not triage_event:
        return {
            "status": "IDLE",
            "reason": "No monitoring.triage.ready event available"
        }

    # ── Idempotency (avoid re-processing same event) ──────────────────────
    last_processed = state.get("investigation_last_event_id")

    if last_processed == triage_event["event_id"]:
        return {
            "status": "SKIPPED",
            "reason": "Event already processed",
            "event_id": last_processed
        }

    triage_payload = triage_event.get("payload", {})

    domain        = triage_payload.get("domain_triage", "CROSS_DOMAIN")
    priority_flag = triage_payload.get("priority_flag", "P1")   # ✅ FIXED

    # ── Scenario selection (can expand later using priority_flag) ─────────
    scenario = "UC_MULTI_DOMAIN_RCA" if domain == "CROSS_DOMAIN" else "UC_SINGLE_RAN"

    rca_output = generate_investigation_output(scenario=scenario)

    source_id = triage_event["event_id"]

    # ── Publish event ────────────────────────────────────────────────────
    event = make_rca_confirmed_event(
        source_event_id = source_id,
        rca_output      = rca_output,
    )

    publish_event(state, event)

    # ── Persist state ────────────────────────────────────────────────────
    state["investigation_last_event_id"] = triage_event["event_id"]
    state["investigation_output"] = event

    print("\n[InvestigationAgent OUTPUT]")
    print(json.dumps({
        "event": event,
        "rca_output": rca_output
    }, indent=2))

    return {
        "published_event":  event["event_type"],
        "event_id":         event["event_id"],
        "confirmed_domain": rca_output["rootCauseAnalysis"]["domain"],
        "rca_branches":     len(rca_output.get("confirmedRcaBranches", [])),
        "severity":         rca_output["rootCauseAnalysis"]["severity"],
        "priority_flag":    priority_flag,  
        "next_agent":       "SolutionPlanningAgent",
        "network_status":   "HEALING",
    }