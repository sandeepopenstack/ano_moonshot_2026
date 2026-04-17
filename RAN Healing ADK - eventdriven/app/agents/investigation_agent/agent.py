from google.adk.agents import Agent
from .tools import run_investigation_mock


class InvestigationAgent(Agent):
    """
    Stage 6 — Investigation Agent

    TRUE A2A behavior:
    - Subscribes to monitoring.triage.ready
    - Runs RCA (mock here)
    - Publishes investigation.rca.confirmed
    """

    def run(self, context):
        print("Running InvestigationAgent")  # debug visibility
        return run_investigation_mock(context)


# ADK agent instance
investigation_agent = InvestigationAgent(
    name="InvestigationAgent",
    description="Consumes monitoring triage and produces confirmed root cause analysis.",
)