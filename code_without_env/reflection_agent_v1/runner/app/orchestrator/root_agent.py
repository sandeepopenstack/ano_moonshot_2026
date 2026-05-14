"""ADK root agent for the standalone ReflectionAgent service.

Reflection is deterministic, so the ADK root calls its two tools directly:
check_execution_result followed by evaluate_and_publish. Cross-agent handoff is
handled by HTTP A2A payloads, not by importing the other agents.
"""

from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types
from typing_extensions import override

from app.reflection_agent.tools import check_execution_result, evaluate_and_publish


class _DirectCtx:
    def __init__(self, state: dict):
        self.state = state


class ReflectionRootAgent(BaseAgent):
    @override
    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        direct_ctx = _DirectCtx(ctx.session.state)
        check_result = check_execution_result(direct_ctx)

        if "CHECK_ERROR" in check_result or "CHECK_SKIPPED" in check_result:
            yield Event(
                author=self.name,
                actions=EventActions(state_delta=dict(ctx.session.state)),
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=check_result)],
                ),
            )
            return

        eval_result = evaluate_and_publish(direct_ctx)
        yield Event(
            author=self.name,
            actions=EventActions(state_delta=dict(ctx.session.state)),
            content=types.Content(
                role="model",
                parts=[types.Part(text=eval_result)],
            ),
        )


root_agent = ReflectionRootAgent(name="ReflectionRootAgent")
