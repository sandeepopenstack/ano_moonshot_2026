# ANO Moonshot 2026 — RAN Self-Healing Agentic Pipeline

Multi-agent **RAN Self-Healing** proof-of-concept built on [Google ADK](https://google.github.io/adk/). Three independent LLM agents (Reflex, Engineer, Reflection) chained by an orchestrator to detect, diagnose, and heal RAN/Core network faults — no human in the loop.

---

## Pipeline Flow

```
Failure Injection → ReflexAgent (LLM: triage via GNN/Spanner)
                 → DetectiveAgent (mock: root cause analysis)
                 → EngineerAgent (LLM: generate healing plan)
                 → ExecutorAgent (mock: execute remediation)
                 → ReflectionAgent (LLM: verify resolution)
                 → RESOLVED or RETRIGGER (max 30 loops)
```

### Supported Use Cases

| ID | Fault | Domain |
|----|-------|--------|
| `uc1` | Antenna Tilt Misconfiguration | RAN (Coverage Hole) |
| `uc2` | HSS Subscriber DB Saturation | Core (HSS Failover) |

---

## Project Structure

```
├── ran_healing_shared/          # Installable shared package (events, config, providers)
│   ├── events.py                # Event bus contracts & helpers
│   ├── remediation_config.py    # Healing configurations & TMF mappings
│   ├── failure_injection_ms.py  # Network failure simulation
│   ├── gnn_inference_provider.py
│   └── providers/
│       ├── detective_provider.py  # Mock RCA provider
│       └── execution_provider.py  # Mock execution provider
│
├── reflex_agent/                # Agent 1: triage & classification
│   ├── agent.py                 # ADK agent definition (LlmAgent)
│   ├── tools.py                 # GNN query → triage → publish
│   ├── requirements.txt         # + ran_healing_shared .whl
│   └── Dockerfile               # Cloud Run container
│
├── engineer_agent/              # Agent 2: healing plan generation
│   ├── agent.py
│   ├── tools.py
│   ├── requirements.txt
│   └── Dockerfile
│
├── reflection_agent/            # Agent 3: verification & compliance
│   ├── agent.py
│   ├── tools.py
│   ├── requirements.txt
│   └── Dockerfile
│
├── orchestrator.py              # Pipeline orchestrator (ADK REST API)
├── test.py                      # No-LLM pipeline test (instant)
├── test_live.py                 # Live LLM pipeline test
├── pyproject.toml               # Build config for ran_healing_shared
├── .env                         # GCP & model configuration
├── AGENTS.md                    # OpenCode agent instructions
├── mcp-toolbox/                 # MCP Toolbox for Spanner
├── TMF-APIS/                    # TMF OpenAPI specs (628, 640, 641, 642, 688, 702, 915, 921)
└── logs/                        # Test run logs & summaries
```

---

## Quick Start

### Prerequisites

- Python 3.12+
- Google ADK CLI: `pip install google-adk[vertexai]`
- GCP project with Vertex AI enabled (or Gemini API key)
- (Optional) Spanner instance for real GNN queries

### 1. Install & Run Locally

```bash
# Install shared package + dependencies
pip install -e ./ran_healing_shared/

# No-LLM test (instant, pure Python — no API calls)
python test.py
USE_CASE_ID=uc2 python test.py

# Live LLM test (requires Vertex AI or GEMINI_API_KEY)
python test_live.py
USE_CASE_ID=uc2 python test_live.py
```

### 2. Start the ADK API Server (for REST pipeline)

```bash
# Start the server — serves all agents registered in subdirectories
adk api_server --port=8000 /path/to/repo/root

# Or start via tmux (already running on port 8000):
# tmux new-session -d -s adk-server adk api_server --port=8000 /path/to/repo/root
```

### 3. Run the Orchestrator (REST pipeline)

```bash
# Uses ADK_BASE_URL=http://localhost:8000 by default
python orchestrator.py
USE_CASE_ID=uc2 python orchestrator.py
```

### 4. (Optional) Start MCP Toolbox for Spanner

```bash
./start_mcp_toolbox.sh   # Terminal 1: starts MCP Toolbox on port 5000
```

---

## API Architecture

```
                  ┌──────────────────────────────┐
                  │      orchestrator.py          │
                  │  (deterministic event router) │
                  └──────┬───────┬───────┬───────┘
                         │       │       │
              ┌──────────┘       │       └──────────┐
              ▼                  ▼                  ▼
     ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
     │ ReflexAgent  │  │EngineerAgent │  │Reflection    │
     │ (LLM triage  │  │(LLM healing  │  │Agent (LLM    │
     │  via GNN)    │  │ plan gen)    │  │ verification)│
     └──────────────┘  └──────────────┘  └──────────────┘
              │                  │                  │
              ▼                  ▼                  ▼
     ┌─────────────────────────────────────────────────┐
     │          ADK REST API (POST /run)                │
     │  Create Session → Seed State → Run → Get State  │
     └─────────────────────────────────────────────────┘
```

Each agent is a standalone ADK `LlmAgent` deployed independently. The orchestrator calls them via `POST /run` with session state propagation (no Pub/Sub).

### ADK API Endpoints Used

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/apps/{app}/users/{user}/sessions` | POST | Create session with initial state |
| `/run` | POST | Invoke agent (`RunAgentRequest`) |
| `/apps/{app}/users/{user}/sessions/{id}` | GET | Read updated session state |

---

## Deployment

### Local Dev (single server)

All agents register on one ADK server; the orchestrator points to `ADK_BASE_URL=http://localhost:8000`.

### Cloud Run (3 independent services)

```bash
adk deploy cloud_run \
  --project=$PROJECT --region=us-central1 \
  --service_name=ran-reflex-agent --with_ui \
  reflex_agent/ \
  -- --build-service-account=$BUILD_SA --quiet

# Then set env vars per service:
gcloud run services update ran-reflex-agent \
  --update-env-vars=GOOGLE_GENAI_USE_VERTEXAI=true,AGENT_MODEL=gemini-2.5-flash
```

Orchestrator picks up Cloud Run URLs via env vars:

```bash
REFLEX_AGENT_URL=https://ran-reflex-agent-xxx.a.run.app \
ENGINEER_AGENT_URL=https://ran-engineer-agent-xxx.a.run.app \
REFLECTION_AGENT_URL=https://ran-reflection-agent-xxx.a.run.app \
python orchestrator.py
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_CLOUD_PROJECT` | — | GCP project ID |
| `GOOGLE_CLOUD_LOCATION` | `us-central1` | GCP region |
| `GOOGLE_GENAI_USE_VERTEXAI` | `true` | Use Vertex AI (vs Gemini API key) |
| `AGENT_MODEL` | `gemini-2.5-flash` | LLM model for all agents |
| `SPANNER_INSTANCE` | `verizon-gnn` | Spanner instance (ReflexAgent) |
| `SPANNER_DATABASE` | `syndata` | Spanner database |
| `TOOLBOX_URL` | `http://localhost:5000` | MCP Toolbox endpoint |
| `MAX_PIPELINE_LOOPS` | `12` | Max retry iterations |
| `USE_CASE_ID` | `uc1` | Fault scenario (`uc1` or `uc2`) |
| `ADK_BASE_URL` | `http://localhost:8000` | ADK API server base URL |
| `{NAME}_AGENT_URL` | — | Per-agent Cloud Run URL override |

---

## Testing

| Command | What it tests | LLM calls? | Time |
|---------|---------------|------------|------|
| `python test.py` | All 6 pipeline steps (pure logic) | No | <1s |
| `python test_live.py` | Full pipeline with Gemini | Yes | ~30s |
| `USE_CASE_ID=uc2 python test.py` | Core fault scenario | No | <1s |
| `python orchestrator.py` | REST API pipeline via ADK server | Yes | ~30s |

Tests produce detailed logs in `logs/` with JSON summaries.

---

## Architecture Rules

- **Event bus = session state** — `state["event_bus"]` list with `latest_{event_type}` keys. No Pub/Sub.
- **`events.py`** is the lowest-level module — must NEVER import from agents or orchestrator.
- **Orchestrator** routes events deterministically (if/elif on event type). Sub-agents use LLM; mocks call tools directly.
- **ReflectionAgent** (v6+) calls tools directly (no LLM loop) to avoid timeout hangs.
- **Mocks → Production**: swap `providers/*_provider.py` with real API clients. No agent logic changes.
- **Import isolation**: `ran_healing_shared` is a pip-installable package. Agents import from it — no `sys.path.insert` needed.

---

## Mocks & Production Swaps

| Mock | Real Implementation | Swap Location |
|------|-------------------|---------------|
| `detective_provider.py` | Ericsson A2A Detective API | `ran_healing_shared/providers/` |
| `execution_provider.py` | Ericsson EIC API | `ran_healing_shared/providers/` |
| `gnn_inference_provider.py` | Real GNN Inference | `ran_healing_shared/` |
| `failure_injection_ms.py` | OSS/NFV event stream | `ran_healing_shared/` |
| Spanner MCP Toolbox | ReflexAgent direct Spanner | MCP Toolbox or `call_gnn_engine` fallback |
