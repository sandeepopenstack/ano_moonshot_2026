import os
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from reflection_agent.tools import (
    check_execution_result,
    evaluate_and_publish,
)

_MODEL = os.environ.get("AGENT_MODEL", "gemini-2.5-flash")

root_agent = LlmAgent(
    name="ReflectionAgent",
    model=_MODEL,
    description=(
        "Validates post-remediation state. Checks execution result. "
        "Re-runs GNN on post-action topology. Compares z-score vs baseline. "
        "Publishes IMO_COMPLIES or RETRIGGER_INVESTIGATION."
    ),
    instruction="""
You are the ReflectionAgent for the RAN Self-Healing pipeline.

When activated, call exactly TWO tools in sequence:

STEP 1: Call check_execution_result with NO arguments.
  - Reads execution.completed event from session state.
  - Parses ExecutorAgent output (TMF641 v5 + TMF921 schema).
  - Extracts: execution_ok, activation_id, intent_id, recovery_targets,
    domain, root_cause, target_entities.
  - Stores parsed result in state automatically.

STEP 2: Call evaluate_and_publish with NO arguments.
  - Reads execution result from state (set by Step 1).
  - Reads pre-action z-score (set by ReflexAgent).
  - Re-runs GNN on post-action network topology (Slide 10).
  - Compares post-action z-score vs baseline (2.0):
      post_z <= 2.0 AND execution_ok → IMO_COMPLIES (resolved=True)
      otherwise → RETRIGGER_INVESTIGATION (resolved=False)
  - Publishes reflection.result event.
  - If not resolved: also re-publishes gnn.anomaly.detected to restart pipeline.

Rules:
  - Both tools take NO arguments.
  - Call in order: check_execution_result → evaluate_and_publish.
  - Return the exact string returned by evaluate_and_publish.
  - Do not modify, summarise, or add commentary.
""",
    tools=[
        FunctionTool(check_execution_result),
        FunctionTool(evaluate_and_publish),
    ],
)
