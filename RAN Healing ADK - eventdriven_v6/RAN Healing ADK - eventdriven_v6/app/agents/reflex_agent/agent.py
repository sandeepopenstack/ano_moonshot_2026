"""
app/agents/reflex_agent/agent.py
==================================
ReflexAgent — 3 function tools, LlmAgent.

Tools execute in strict order:
  1. call_gnn_engine  — tools call to GNN, stores result in state
  2. perform_triage   — MCP/Spanner call, performs domain triage
  3. publish_triage   — publishes reflex.triage.ready to event bus

No MCPToolset registered here — MCP is called directly via requests
in perform_triage (see tools.py _query_via_mcp_toolbox).
This avoids the @modelcontextprotocol/server-google-spanner npm issue.
"""

import os
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from app.agents.reflex_agent.tools import (
    call_gnn_engine,
    perform_triage,
    publish_triage,
)

_MODEL = os.environ.get("AGENT_MODEL", "gemini-2.5-flash")

reflex_agent = LlmAgent(
    name="ReflexAgent",
    model=_MODEL,
    description=(
        "Receives anomaly trigger. Makes tools call to GNN Inference Engine. "
        "Queries Spanner via MCP for entity domain metadata and impact radius. "
        "Performs domain triage. Publishes reflex.triage.ready to Detective Agent."
    ),
    instruction="""
You are the ReflexAgent for the RAN Self-Healing pipeline.

When activated, call exactly THREE tools in this order — no deviations:

STEP 1: Call call_gnn_engine with NO arguments.
  - Makes a tools call (JSON payload) to GNN Inference Engine (Slide 4a).
  - GNN reads Spanner graph internally and returns (Slide 4b):
      * Anomalous subgraph (nodes + edges from graph traversal)
      * Per-node anomaly scores (z-score, impact_score, criticality from INSIGHT.csv)
      * Composite impact score (GNN × subscribers × revenue × ToD × app_type)
      * Ranked remediation branches (highest business impact first)
  - Result is stored in session state automatically.

STEP 2: Call perform_triage with NO arguments.
  - Reads GNN response from session state.
  - Makes MCP/tools call to Spanner (Slide 5):
      * query_entity_domains → entity table (domain of each affected node)
      * query_entity_connections → edge_entitytoentity (impact radius)
      * query_neighbor_cells → edge_entitytoneighbor (blast radius)
  - Performs domain triage: identifies RAN / CORE / TRANSPORT / CROSS_DOMAIN.
  - Maps GNN ranked branches to domain priority: HIGH / MEDIUM / LOW.
  - Prepares 6 fields for Detective Agent.

STEP 3: Call publish_triage with NO arguments.
  - Reads triage result from session state.
  - Publishes reflex.triage.ready event with 6 fields for Detective Agent.

Rules:
  - All three tools take NO arguments.
  - Call in order: call_gnn_engine → perform_triage → publish_triage.
  - Do NOT skip any step.
  - Do NOT add commentary or modify tool return values.
  - Return the exact string returned by publish_triage.
""",
    tools=[
        FunctionTool(call_gnn_engine),
        FunctionTool(perform_triage),
        FunctionTool(publish_triage),
    ],
)