from typing import AsyncGenerator
from typing_extensions import override

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from .tools import run_execution_mock


class _Ctx:
    def __init__(self, state): self.state = state


class ExecutionAgent(BaseAgent):

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        print("Running ExecutionAgent")
        result = run_execution_mock(_Ctx(ctx.session.state))
        yield Event(
            author=self.name,
            actions=EventActions(state_delta=dict(ctx.session.state)),
            content=types.Content(role="model", parts=[types.Part(text=str(result))])
        )


execution_agent = ExecutionAgent(
    name="ExecutionAgent",
    description="Executes TMF641 remediation sub-orders across RAN, CORE, TRANSPORT.",
)