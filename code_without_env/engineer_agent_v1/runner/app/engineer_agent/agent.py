"""
app/agents/engineer_agent/agent.py
====================================
EngineerAgent — single tool LlmAgent.

One tool only: generate_healing_plan
  - Reads detective.rca.confirmed from session state.
  - Scores branches with utility formula (slide 7).
  - Builds TMF921 remediation intent.
  - Publishes engineer.ready.

Single-tool design avoids multi-tool dict-passing issues.
"""

import os
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from app.engineer_agent.tools import generate_healing_plan

_MODEL = os.environ.get("AGENT_MODEL", "gemini-2.5-flash")

engineer_agent = LlmAgent(
    name="EngineerAgent",
    model=_MODEL,
    description=(
        "Reads confirmed RCA from Detective Agent. Scores healing branches using "
        "utility formula: impact × criticality × (1-risk) × reversibility. "
        "Builds TMF921 remediation intent. Publishes engineer.ready."
    ),
    instruction="""
You are the EngineerAgent for the RAN Self-Healing pipeline.

When activated, call generate_healing_plan exactly ONCE with NO arguments.

The tool reads detective.rca.confirmed from session state automatically and:
  1. Parses the Detective Agent RCA output:
       - root_cause, domain, affected_entities, causal_parameters
       - recovery_targets (from PERFORMANCE.csv normal vs degraded KPI values)
       - risk_score, reversibility_score (from CHANGEREQUEST.csv per scenario)
  2. Fetches business metadata per entity (criticality tier, traffic density).
  3. Scores each healing branch using the utility formula (Slide 7):
       Utility = impact × criticality × (1 - risk) × reversibility
         impact        → kpi_delta_pct from Detective Agent
         criticality   → GNN impact_score from INSIGHT.csv (stored in state)
         risk          → risk_score from CHANGEREQUEST.csv
         reversibility → reversibility_score from CHANGEREQUEST.csv
  4. Ranks branches by utility score (highest first → sequence 1).
  5. Builds TMF921 intent with recovery target expressions.
  6. Publishes engineer.ready for the Executor Agent.

Rules:
  - Call generate_healing_plan with NO arguments.
  - Call it exactly once.
  - Return the result exactly as the tool returns it.
  - Do not modify, summarise, or add commentary.
""",
    tools=[FunctionTool(generate_healing_plan)],
)
