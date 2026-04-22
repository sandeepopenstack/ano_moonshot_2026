from google.adk.tools import ToolContext
import json

from execution_mock_output import generate_execution_output

from app.events import (
    EVT_SOLUTION_PLAN_READY,
    consume_latest,
    publish_event,
    make_execution_completed_event,
)


def run_execution_mock(tool_context: ToolContext) -> dict:
    """
    Stage 8/9 — Execution Agent (mock)

    Subscribes to: solution.plan.ready
    Publishes:     execution.completed
    """

    state = tool_context.state

    # ── Subscribe ────────────────────────────────────────────────────────
    plan_event = consume_latest(state, EVT_SOLUTION_PLAN_READY)

    if not plan_event:
        return {
            "status": "IDLE",
            "reason": "No solution.plan.ready event found"
        }

    # ── Idempotency ──────────────────────────────────────────────────────
    last_processed = state.get("execution_last_event_id")

    if last_processed == plan_event["event_id"]:
        return {
            "status": "SKIPPED",
            "reason": "Event already processed",
            "event_id": last_processed
        }

    plan_payload = plan_event["payload"]
    source_id    = plan_event["event_id"]

    # ── Derive scenario from actual domains in the healing plan ──────────
    domains = {b["domain"] for b in plan_payload.get("healing_branches", [])}

    if "CORE" in domains and "RAN" not in domains:
        scenario = "UC2_CORE_REMEDIATION"
    else:
        scenario = "UC1_SUCCESSFUL_REMEDIATION"

    # ── Generate execution output ────────────────────────────────────────
    execution_output = generate_execution_output(scenario=scenario)

    print("\n================ EXECUTION AGENT OUTPUT ================")
    print("Event Type: execution.completed")
    print("\nRAW PAYLOAD ↓↓↓")
    print(json.dumps(execution_output, indent=2))
    print("=======================================================\n")

    # ── Publish event ────────────────────────────────────────────────────
    event = make_execution_completed_event(
        source_event_id  = source_id,
        execution_output = execution_output,
    )

    publish_event(state, event)

    # ── Persist state ────────────────────────────────────────────────────
    state["execution_last_event_id"] = plan_event["event_id"]
    state["execution_output"] = execution_output

    # ── Summary ──────────────────────────────────────────────────────────
    branches = execution_output["executionBranches"]
    all_ok   = all(b["status"] == "SUCCESS" for b in branches)

    return {
        "status":           "EVENT_PUBLISHED",
        "published_event":  event["event_type"],
        "event_id":         event["event_id"],
        "execution_status": execution_output["executionStatus"],
        "branches_ok":      all_ok,
        "branch_results":   {b["domain"]: b["status"] for b in branches},
        "next_agent":       "ValidationAgent",
        "network_status":   "HEALING",
    }