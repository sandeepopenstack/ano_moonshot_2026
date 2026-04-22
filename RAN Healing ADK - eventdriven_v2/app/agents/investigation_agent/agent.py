from typing import AsyncGenerator
from typing_extensions import override

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from .tools import run_investigation_mock


class _Ctx:
    def __init__(self, state): self.state = state


class InvestigationAgent(BaseAgent):

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        print("Running InvestigationAgent")
        result = run_investigation_mock(_Ctx(ctx.session.state))
        yield Event(
            author=self.name,
            actions=EventActions(state_delta=dict(ctx.session.state)),
            content=types.Content(role="model", parts=[types.Part(text=str(result))])
        )


investigation_agent = InvestigationAgent(
    name="InvestigationAgent",
    description="Performs root cause analysis on monitoring triage output.",
)