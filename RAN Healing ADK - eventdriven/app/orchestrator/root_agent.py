from google.adk.agents import Agent

from app.agents.monitoring_agent.tools        import monitor_and_triage
from app.agents.investigation_agent.tools     import run_investigation_mock
from app.agents.solution_planning_agent.tools import generate_healing_plan
from app.agents.execution_agent.tools         import run_execution_mock
from app.agents.validation_agent.tools        import validate_remediation

from app.events import (
    EVT_GNN_ANOMALY_DETECTED,
    EVT_MONITORING_TRIAGE_READY,
    EVT_INVESTIGATION_RCA_CONFIRMED,
    EVT_SOLUTION_PLAN_READY,
    EVT_EXECUTION_COMPLETED,
    EVT_VALIDATION_RESULT,
    consume_latest,
)


class RootAgent(Agent):
    def run(self, context):
        state = context.state

        # Get latest event from event bus
        event_bus = state.get("event_bus", [])
        if not event_bus:
            return {"status": "IDLE"}

        latest_event = event_bus[-1]
        event_type   = latest_event["event_type"]

        print(f"\n[Orchestrator] Routing event: {event_type}")

        # ── STRICT EVENT ROUTING ───────────────────────────
        if event_type == EVT_GNN_ANOMALY_DETECTED:
            return monitor_and_triage(context)

        elif event_type == EVT_MONITORING_TRIAGE_READY:
            return run_investigation_mock(context)

        elif event_type == EVT_INVESTIGATION_RCA_CONFIRMED:
            return generate_healing_plan(context)

        elif event_type == EVT_SOLUTION_PLAN_READY:
            return run_execution_mock(context)

        elif event_type == EVT_EXECUTION_COMPLETED:
            return validate_remediation(context)

        elif event_type == EVT_VALIDATION_RESULT:
            resolved = latest_event.get("resolved", False)

            if resolved:
                print("\n[Orchestrator] RESOLVED — Workflow completed")
                return {"status": "DONE"}

            else:
                print("\n[Orchestrator]  RETRIGGERING FLOW")
                return monitor_and_triage(context)

        return {"status": "UNKNOWN_EVENT"}


root_agent = RootAgent(name="RanHealingOrchestrator")