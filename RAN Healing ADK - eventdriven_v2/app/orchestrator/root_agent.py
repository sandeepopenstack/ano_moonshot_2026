from typing import AsyncGenerator
from typing_extensions import override

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

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
)


class _Ctx:
    def __init__(self, state: dict):
        self.state = state


# ── Stage label map for clean logging ─────────────────────────────────────────
_STAGE_LOG = {
    EVT_GNN_ANOMALY_DETECTED:        ("Stage 4→5", "GNN anomaly detected   → routing to MonitoringAgent"),
    EVT_MONITORING_TRIAGE_READY:     ("Stage 5→6", "Monitoring triage done → routing to InvestigationAgent"),
    EVT_INVESTIGATION_RCA_CONFIRMED: ("Stage 6→7", "RCA confirmed          → routing to SolutionPlanningAgent"),
    EVT_SOLUTION_PLAN_READY:         ("Stage 7→9", "Solution plan ready    → routing to ExecutionAgent"),
    EVT_EXECUTION_COMPLETED:         ("Stage 9→10","Execution complete     → routing to ValidationAgent"),
    EVT_VALIDATION_RESULT:           ("Stage 10",  "Validation result received"),
}

# ── Tool route map ─────────────────────────────────────────────────────────────
_ROUTE = {
    EVT_GNN_ANOMALY_DETECTED:        monitor_and_triage,
    EVT_MONITORING_TRIAGE_READY:     run_investigation_mock,
    EVT_INVESTIGATION_RCA_CONFIRMED: generate_healing_plan,
    EVT_SOLUTION_PLAN_READY:         run_execution_mock,
    EVT_EXECUTION_COMPLETED:         validate_remediation,
}


class RanHealingOrchestrator(BaseAgent):
    """
    Event-driven pipeline orchestrator.

    Role: Observer + Event Router.
      - OBSERVES every event published to the event bus
      - LOGS what it sees (stage, domain, priority)
      - ROUTES to the correct agent tool based on event type
      - Does NOT make decisions — routing is purely deterministic
      - Does NOT use LLM — pure Python if/else

    In a future full A2A deployment each tool becomes a separate Cloud Run service and this routing becomes
    Pub/Sub subscriptions. The event contracts (events.py) stay identical — only the transport layer changes.
    """

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:

        state     = ctx.session.state
        event_bus = state.get("event_bus", [])

        # ── Observe: nothing in bus yet ────────────────────────────────────
        if not event_bus:
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="[Observer] Event bus empty — waiting")]
                )
            )
            return

        latest_event = event_bus[-1]
        event_type   = latest_event["event_type"]
        network_status = state.get("network_status", "UNKNOWN")

        # ── Observe + Log ──────────────────────────────────────────────────
        stage_label, stage_desc = _STAGE_LOG.get(
            event_type, ("Unknown", event_type)
        )
        print(f"\n[Observer] {stage_label}  |  {network_status}")
        print(f"           {stage_desc}")
        print(f"           event_id={latest_event.get('event_id', 'n/a')[:8]}...")

        tool_ctx = _Ctx(state)

        # ── Route: deterministic, no LLM ──────────────────────────────────
        if event_type in _ROUTE:
            result = _ROUTE[event_type](tool_ctx)

        elif event_type == EVT_VALIDATION_RESULT:
            resolved = latest_event.get("resolved", False)
            if resolved:
                print(f"\n[Observer] Pipeline RESOLVED — all branches healed")
                result = {"status": "RESOLVED"}
            else:
                # ValidationAgent tools.py already re-published gnn.anomaly.detected
                # to the event bus when not resolved. The next loop iteration in
                # main.py will see that new event and route it to MonitoringAgent.
                print(f"\n[Observer] Retrigger event published by ValidationAgent"
                      f" — next loop handles it")
                result = {"status": "RETRIGGER_PUBLISHED"}

        else:
            print(f"\n[Observer] Unknown event type: {event_type}")
            result = {"status": "UNKNOWN_EVENT", "event_type": event_type}

        # ── Emit ADK event with full state delta ───────────────────────────
        yield Event(
            author=self.name,
            actions=EventActions(state_delta=dict(state)),
            content=types.Content(
                role="model",
                parts=[types.Part(text=str(result))]
            )
        )


# ADK discovers this variable by name — must be exactly root_agent
root_agent = RanHealingOrchestrator(name="RanHealingOrchestrator")