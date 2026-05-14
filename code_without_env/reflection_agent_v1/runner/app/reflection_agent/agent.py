"""ReflectionAgent definition.

The deployed Reflection service calls the tools directly because the validation
logic is deterministic. The LlmAgent is kept as an ADK-compatible agent
definition and documentation of the two-tool contract.
"""

import os

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from app.reflection_agent.tools import check_execution_result, evaluate_and_publish


_MODEL = os.environ.get("AGENT_MODEL", "gemini-2.5-flash")

reflection_agent = LlmAgent(
    name="ReflectionAgent",
    model=_MODEL,
    description=(
        "Validates execution output, anomaly labels, and KPI degradation state. "
        "Publishes IMO_COMPLIES or RETRIGGER_INVESTIGATION."
    ),
    instruction="""
You are the ReflectionAgent for the RAN Self-Healing pipeline.

When activated, call exactly TWO tools in sequence:

STEP 1: Call check_execution_result with NO arguments.
  - Reads execution.completed event from session state.
  - Parses ExecutorAgent output using the TMF641/TMF921 payload.
  - Extracts execution_ok, activation_id, intent_id, recovery_targets,
    domain, root_cause, target_entities, and affected_hex_bins.

STEP 2: Call evaluate_and_publish with NO arguments.
  - Reads the parsed execution result from state.
  - Validates three gates:
      gate1: execution_ok
      gate2: anomaly_label is NORMAL from Spanner aw_base_hex07_anom
      gate3: is_degraded is False from Spanner performance
  - All gates pass -> IMO_COMPLIES.
  - Any gate fails -> RETRIGGER_INVESTIGATION with a Step 2b trigger_event
    payload for ReflexAgent.

Rules:
  - Both tools take NO arguments.
  - Call in order: check_execution_result -> evaluate_and_publish.
  - Return the exact string returned by evaluate_and_publish.
  - Do not modify, summarize, or add commentary.
""",
    tools=[
        FunctionTool(check_execution_result),
        FunctionTool(evaluate_and_publish),
    ],
)
