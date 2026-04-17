import json

from app.orchestrator.root_agent import root_agent
from app.events import (
    make_gnn_anomaly_event,
    publish_event,
    NETWORK_STATUS_KEY,
    EVENT_BUS_KEY,
)
from app.workflow_state import extract_final_summary
from gnn_inference_provider import generate_gnn_inference_event


def run_event_driven_pipeline():
    # ── Initial state ─────────────────────────────
    state = {
        NETWORK_STATUS_KEY: "HEALTHY",
        EVENT_BUS_KEY: []
    }

    print("=" * 65)
    print("  RAN Self-Healing — Event-Driven Pipeline")
    print("=" * 65)

    print("\n[Stage 1] Network status: HEALTHY")

    # ── Inject anomaly (Stage 4) ──────────────────
    print("[Stage 4] Injecting anomaly...")

    gnn = generate_gnn_inference_event(scenario="UC_MULTI_DOMAIN_HEALING")
    event = make_gnn_anomaly_event(gnn)

    publish_event(state, event)

    print(f"Event published: {event['event_type']}")
    print(f"Network status: {state[NETWORK_STATUS_KEY]}")

    # ── Event-driven loop ─────────────────────────
    context = type("Context", (), {"state": state})()

    print("\n[Pipeline] Running event-driven healing...\n")
    print("-" * 65)

    while True:
        result = root_agent.run(context)

        latest_event = state["event_bus"][-1]
        event_type = latest_event["event_type"]

        print(f"\n EVENT: {event_type}")

        # Stop condition
        if event_type == "validation.result":
            if latest_event.get("resolved"):
                print("\n FINAL STATUS: RESOLVED")
                break

        # Safety stop
        if len(state["event_bus"]) > 20:
            print("\n Safety break (too many events)")
            break

    print("-" * 65)

    print("\n[Final Summary]")
    summary = extract_final_summary(state)
    print(json.dumps(summary, indent=2))

    return state


def main():
    run_event_driven_pipeline()


if __name__ == "__main__":
    main()