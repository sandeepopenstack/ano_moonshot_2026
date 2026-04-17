# RAN Self-Healing ADK — GCP Deployment Guide

## Architecture (PPT Aligned)

```
Stage 4  GNN Inference Engine          → triggers MonitoringAgent
Stage 5  MonitoringAgent               → domain triage + priority flag (A2A session state)
Stage 6  InvestigationAgent (mock)     → confirmed RCA branches
Stage 7  SolutionPlanningAgent         → TMF921 remediation intent (A2A session state)
Stage 8  ExecutionAgent (Ericsson)     → TMF641 Sub-Orders to Automation Engine
Stage 9  Automation Engine             → RAN/CORE/TRANSPORT changes
Stage 10 ValidationAgent              → z-score check; IMO_COMPLIES or RETRIGGER
Stage 11 Cloud Logging                → agentic AI logs
```

## How A2A Works in This Implementation

ADK's **session state** is the A2A message bus:

```
MonitoringAgent tool  → writes context.state["monitoring_output"]
                                         ↓ (ADK SequentialAgent passes session)
SolutionPlanningAgent → reads  context.state["monitoring_output"]  (optional cross-check)
                      → writes context.state["solution_output"]
                                         ↓
ValidationAgent       → reads  context.state["solution_output"]    (audit cross-check)
                      → writes context.state["validation_output"]
```

No manual function-to-function calls. No `main.py` sequencing. ADK handles it.

---

## Local Development

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Set credentials
```bash
export GOOGLE_CLOUD_PROJECT="your-gcp-project-id"
export GOOGLE_GENAI_USE_VERTEXAI=TRUE
gcloud auth application-default login
```

### 3a. Run via ADK Dev UI (recommended for debugging)
```bash
adk web
# Opens http://localhost:8000
# Select "RanHealingOrchestrator" and send a message
```

### 3b. Run via script
```bash
python main.py
```

---

## GCP Deployment Options

### Option A — Cloud Run (recommended for production)
```bash
# Build and deploy
adk deploy cloud_run \
  --project=$GOOGLE_CLOUD_PROJECT \
  --region=us-central1 \
  --service-name=ran-healing-adk \
  --with-ui

# The ADK web UI will be served at the Cloud Run URL
```

### Option B — Vertex AI Agent Engine (managed, serverless)
```bash
adk deploy agent_engine \
  --project=$GOOGLE_CLOUD_PROJECT \
  --region=us-central1
```

### Required GCP IAM Roles
```
roles/aiplatform.user          # Vertex AI / Gemini model access
roles/bigquery.dataViewer      # PM counters, FM alarms (Stage 6 production)
roles/spanner.databaseReader   # Synthetic data (Stage 0/1)
roles/pubsub.subscriber        # TMF event stream (production GNN trigger)
roles/logging.logWriter        # Cloud Logging (Stage 11)
```

### Environment Variables (set in Cloud Run or Secret Manager)
```bash
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_LOCATION=us-central1   # Vertex AI region
```

---

## Project Structure

```
ran_healing_adk/
├── app/
│   ├── agents/
│   │   ├── monitoring_agent/
│   │   │   ├── agent.py        # LlmAgent definition
│   │   │   ├── tools.py        # monitor_and_triage (ToolContext)
│   │   │   └── mock_api.py     # Stage 4→5 GNN fetch + Stage 5→6 publish
│   │   ├── solution_planning_agent/
│   │   │   ├── agent.py        # LlmAgent definition
│   │   │   ├── tools.py        # generate_healing_plan (ToolContext)
│   │   │   └── mock_api.py     # Stage 6→7 RCA fetch + Stage 7→8 publish
│   │   └── validation_agent/
│   │       ├── agent.py        # LlmAgent definition
│   │       ├── tools.py        # validate_remediation (ToolContext)
│   │       └── mock_api.py     # Stage 9→10 execution result fetch
│   └── orchestrator/
│       └── root_agent.py       # SequentialAgent (true A2A orchestrator)
├── gnn_inference_provider.py   # Stage 4 mock (swap for Vertex AI endpoint)
├── investigation_mock_output.py # Stage 6 mock (swap for Ericsson A2A API)
├── execution_mock_output.py    # Stage 9 mock (swap for Ericsson Execution API)
├── main.py                     # ADK Runner entry point
├── requirements.txt
└── README.md
```

---

## Swapping Mocks for Production

| Mock file | Production replacement |
|---|---|
| `gnn_inference_provider.py` | Vertex AI GNN endpoint / Spanner Graph inference |
| `investigation_mock_output.py` | Ericsson InvestigationAgent A2A API |
| `execution_mock_output.py` | Ericsson ExecutionAgent TMF641 status API |

Each swap only requires changing the `fetch_*` function in the corresponding `mock_api.py`. Agent logic and session state A2A flow remain unchanged.
