from typing import AsyncGenerator
from typing_extensions import override

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from .tools import validate_remediation


class _Ctx:
    def __init__(self, state): self.state = state


class ValidationAgent(BaseAgent):

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        print("Running ValidationAgent")
        result = validate_remediation(_Ctx(ctx.session.state))
        yield Event(
            author=self.name,
            actions=EventActions(state_delta=dict(ctx.session.state)),
            content=types.Content(role="model", parts=[types.Part(text=str(result))])
        )


validation_agent = ValidationAgent(
    name="ValidationAgent",
    description="Validates post-remediation z-score and topology state.",
)