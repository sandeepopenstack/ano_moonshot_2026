"""
app/orchestrator/root_agent.py
================================
Event-driven orchestrator.

AGENT EXECUTION STRATEGY:
  ReflexAgent     → LlmAgent.run_async() — needs LLM to sequence 3 tools
  EngineerAgent   → LlmAgent.run_async() — needs LLM to call generate_healing_plan
  ReflectionAgent → tools called DIRECTLY — deterministic logic, no LLM needed
    check_execution_result() reads execution state — no reasoning needed
    evaluate_and_publish()   checks z-score vs baseline — pure math
    LlmAgent for ReflectionAgent causes timeout hangs in loops 5-8

  DetectiveAgent (other team, Ericsson) → mock called directly in orchestrator
  ExecutorAgent     (other team, Ericsson) → mock called directly in orchestrator
  Both yield EventActions(state_delta) so ADK persists state correctly.


"""

import logging
from typing import AsyncGenerator
from typing_extensions import override

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from app.agents.reflex_agent.agent   import reflex_agent
from app.agents.engineer_agent.agent import engineer_agent

from providers.detective_provider import generate_detective_output
from providers.execution_provider     import generate_execution_output

# Use evaluate_and_publish directly — NOT the evaluate_resolution wrapper
from app.agents.reflection_agent.tools import (
    check_execution_result,
    evaluate_and_publish,
)

from app.events import (
    EVT_FAILURE_NOTIFICATION,
    EVT_REFLEX_TRIAGE_READY,
    EVT_DETECTIVE_RCA_CONFIRMED,
    EVT_ENGINEER_READY,
    EVT_EXECUTION_COMPLETED,
    EVT_REFLECTION_RESULT,
    NETWORK_STATUS_KEY,
    EVENT_BUS_KEY,
    latest_key,
    publish_event,
    make_rca_confirmed_event,
    make_execution_completed_event,
)


def _get_stage(state: dict) -> str:
    """
    Read current pipeline stage from state.
    Checks highest stage first (reflection → execution → engineer → ...).
    Does NOT read from bus[-1] — uses latest_key() state keys.
    """
    if state.get(latest_key(EVT_REFLECTION_RESULT)):
        return EVT_REFLECTION_RESULT
    if state.get(latest_key(EVT_EXECUTION_COMPLETED)):
        return EVT_EXECUTION_COMPLETED
    if state.get(latest_key(EVT_ENGINEER_READY)):
        return EVT_ENGINEER_READY
    if state.get(latest_key(EVT_DETECTIVE_RCA_CONFIRMED)):
        return EVT_DETECTIVE_RCA_CONFIRMED
    if state.get(latest_key(EVT_REFLEX_TRIAGE_READY)):
        return EVT_REFLEX_TRIAGE_READY
    return EVT_FAILURE_NOTIFICATION


class _DirectCtx:
    """Minimal context for direct tool calls (ReflectionAgent — no LLM)."""
    def __init__(self, state: dict):
        self.state = state


class RanHealingOrchestrator(BaseAgent):

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:

        state = ctx.session.state

        if not state.get(EVENT_BUS_KEY):
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="[Orchestrator] Event bus empty — nothing to process")],
                ),
            )
            return

        stage = _get_stage(state)
        logging.info(f"[Orchestrator] Stage: {stage}")

        # ── ReflexAgent (LLM — 3 tool sequence) ───────────────────────────
        if stage == EVT_FAILURE_NOTIFICATION:
            async for evt in reflex_agent.run_async(ctx):
                yield evt

        # ── DetectiveAgent (other team — direct mock call) ─────────────
        elif stage == EVT_REFLEX_TRIAGE_READY:
            reflex_event   = state.get(latest_key(EVT_REFLEX_TRIAGE_READY), {})
            reflex_payload = reflex_event.get("payload", {})

            print("\n[DetectiveAgent — Step 6] Running (Ericsson Detective Agent)")
            print(f"  domain   = {reflex_payload.get('domain_triage')}")
            print(f"  priority = {reflex_payload.get('priority')}")
            print(f"  entities = {reflex_payload.get('entity_ids')}")

            rca_output = generate_detective_output(reflex_payload)
            rca_event  = make_rca_confirmed_event(
                source_event_id=reflex_event.get("event_id", ""),
                rca_output=rca_output,
            )
            publish_event(state, rca_event)
            print(f"  root_cause : {rca_output.get('root_cause')}")
            print(f"  confidence : {rca_output.get('confidence_score')}")
            print(f"  risk       : {rca_output.get('risk_score')}")
            print(f"  reversible : {rca_output.get('reversibility_score')}")
            print(f"  Published  : {rca_event['event_type']}")

            yield Event(
                author=self.name,
                actions=EventActions(state_delta=dict(state)),
                content=types.Content(
                    role="model",
                    parts=[types.Part(
                        text=f"DetectiveAgent: {rca_event['event_type']} published"
                    )],
                ),
            )

        # ── EngineerAgent (LLM — single tool) ─────────────────────────────
        elif stage == EVT_DETECTIVE_RCA_CONFIRMED:
            async for evt in engineer_agent.run_async(ctx):
                yield evt

        # ── ExecutorAgent (other team — direct mock call) ─────────────────
        elif stage == EVT_ENGINEER_READY:
            engineer_event   = state.get(latest_key(EVT_ENGINEER_READY), {})
            engineer_payload = engineer_event.get("payload", {})
            tmf921           = engineer_payload.get("tmf921_intent", {})

            print("\n[ExecutorAgent — Step 8] Running (Ericsson EIC)")
            print(f"  intent_id : {tmf921.get('intent_id')}")
            print(f"  priority  : {tmf921.get('priority')}")
            print(f"  targets   : {tmf921.get('target_entities')}")
            print(f"  domain    : {tmf921.get('domain')}")
            print(f"  branches  : {[b.get('branch_id') for b in engineer_payload.get('healing_branches', [])]}")

            exec_output = generate_execution_output(engineer_payload)
            exec_event  = make_execution_completed_event(
                source_event_id=engineer_event.get("event_id", ""),
                execution_output=exec_output,
            )
            publish_event(state, exec_event)
            print(f"  activation_id : {exec_output.get('activation_id')}")
            print(f"  state         : {exec_output.get('state')}")
            print(f"  Published     : {exec_event['event_type']}")

            yield Event(
                author=self.name,
                actions=EventActions(state_delta=dict(state)),
                content=types.Content(
                    role="model",
                    parts=[types.Part(
                        text=f"ExecutorAgent: {exec_event['event_type']} published"
                    )],
                ),
            )

        # ── ReflectionAgent (direct tool calls — no LLM) ──────────────────
        elif stage == EVT_EXECUTION_COMPLETED:
            print("\n[ReflectionAgent — Step 10] Running (direct tool calls)")
            direct_ctx = _DirectCtx(state)

            # Tool 1: parse execution result
            check_result = check_execution_result(direct_ctx)
            exec_result  = state.get("reflection_exec_result", {})
            print(f"  check_execution_result: execution_ok={exec_result.get('execution_ok')}")

            # Tool 2: evaluate resolution, re-run GNN, publish reflection.result
            eval_result = evaluate_and_publish(direct_ctx)
            refl_output = state.get("reflection_output", {})

            status   = refl_output.get("status", "UNKNOWN")
            resolved = refl_output.get("resolved", False)

            print(f"  evaluate_and_publish: {eval_result}")
            print(f"  status   : {status}")
            print(f"  resolved : {resolved}")

            if resolved:
                state[NETWORK_STATUS_KEY] = "RESOLVED"

            yield Event(
                author=self.name,
                actions=EventActions(state_delta=dict(state)),
                content=types.Content(
                    role="model",
                    parts=[types.Part(
                        text=f"ReflectionAgent: {status} | resolved={resolved}"
                    )],
                ),
            )

        # ── Verdict ────────────────────────────────────────────────────────
        elif stage == EVT_REFLECTION_RESULT:
            reflection_event = state.get(latest_key(EVT_REFLECTION_RESULT), {})
            resolved = reflection_event.get("resolved", False)
            status   = "RESOLVED" if resolved else "RETRIGGER_PUBLISHED"
            if resolved:
                state[NETWORK_STATUS_KEY] = "RESOLVED"
            logging.info(f"[Orchestrator] Verdict: {status}")
            yield Event(
                author=self.name,
                actions=EventActions(state_delta=dict(state)),
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=f"[Orchestrator] {status}")],
                ),
            )

        else:
            yield Event(
                author=self.name,
                actions=EventActions(state_delta=dict(state)),
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=f"UNKNOWN_STAGE: {stage}")],
                ),
            )


root_agent = RanHealingOrchestrator(
    name="RanHealingOrchestrator",
    sub_agents=[reflex_agent, engineer_agent],
)