from google.adk.agents import Agent
from .tools import run_execution_mock


class ExecutionAgent(Agent):
    """
    Stage 8/9 — Execution Agent

    TRUE A2A:
    - Subscribes to solution.plan.ready
    - Executes (mock)
    - Publishes execution.completed
    """

    def run(self, context):
        print("Running ExecutionAgent")
        return run_execution_mock(context)


execution_agent = ExecutionAgent(
    name="ExecutionAgent",
    description="Consumes TMF921 intent and simulates execution across RAN, CORE, TRANSPORT domains.",
)