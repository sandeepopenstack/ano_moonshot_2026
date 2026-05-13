# ANO Moonshot 2026 — RAN Self-Healing Agentic Pipeline

Multi-agent **RAN Self-Healing** proof-of-concept built on [Google ADK](https://google.github.io/adk/). Three independent LLM agents (Reflex, Engineer, Reflection) communicate via **A2A protocol** (peer-to-peer JSON-RPC, no orchestrator) to detect, diagnose, and heal RAN/Core network faults — no human in the loop.

---

## Pipeline Flow

```
FailureInjection → ReflexAgent (LLM: triage via GNN/Spanner)
                → DetectiveAgent (mock: root cause analysis)
                → EngineerAgent (LLM: generate healing plan)
                → ExecutorAgent (mock: execute remediation)
                → ReflectionAgent (LLM: verify resolution)
                → RESOLVED or RETRIGGER (max 30 loops)
```

Agents communicate via **A2A JSON-RPC** over HTTP. Each agent is deployed as a standalone Cloud Run service exposing A2A endpoints.

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
│   ├── agent.json               # A2A Agent Card (v0.3 Pydantic)
│   ├── __init__.py              # Package marker
│   ├── requirements.txt         # + ran_healing_shared .whl (abs path)
│   └── ran_healing_shared-0.1.0-py3-none-any.whl  # Pre-built shared wheel
│
├── engineer_agent/              # Agent 2: healing plan generation
│   ├── agent.py
│   ├── tools.py
│   ├── agent.json
│   ├── __init__.py
│   ├── requirements.txt
│   └── ran_healing_shared-0.1.0-py3-none-any.whl
│
├── reflection_agent/            # Agent 3: verification & compliance
│   ├── agent.py
│   ├── tools.py
│   ├── agent.json
│   ├── __init__.py
│   ├── requirements.txt
│   └── ran_healing_shared-0.1.0-py3-none-any.whl
│
├── tests/                       # Test scripts
│   ├── test.py                  # No-LLM pipeline test (instant)
│   ├── test_live.py             # Live LLM pipeline test
│   ├── test_deployed_agents.py  # Health check all 3 deployed services
│   ├── test_invoke_all.py       # Invoke all 3 via ADK API
│   └── test_reflex_invoke.py    # Invoke reflex agent only
│
├── orchestrator.py              # (Deprecated) Old centralized orchestrator
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

- Python 3.11+
- Google ADK CLI: `pip install google-adk[vertexai]`
- GCP project with Vertex AI enabled (or Gemini API key)
- (Optional) Spanner instance for real GNN queries

### 1. Install & Run Locally (no-LLM test)

```bash
# Install shared package + dependencies
pip install -e ./ran_healing_shared/

# No-LLM test (instant, pure Python — no API calls)
python tests/test.py
USE_CASE_ID=uc2 python tests/test.py

# Live LLM test (requires Vertex AI or GEMINI_API_KEY)
python tests/test_live.py
USE_CASE_ID=uc2 python tests/test_live.py
```

### 2. Build Shared Wheel (for deployment)

```bash
pip install build
python -m build
cp dist/ran_healing_shared-0.1.0-py3-none-any.whl reflex_agent/
cp dist/ran_healing_shared-0.1.0-py3-none-any.whl engineer_agent/
cp dist/ran_healing_shared-0.1.0-py3-none-any.whl reflection_agent/
```

### 3. Deploy to Cloud Run

Each agent deploys independently with A2A enabled. Critical requirements:
- `agent.json` must exist with v0.3 Pydantic AgentCard schema (includes `url` field)
- `requirements.txt` must pin `a2a-sdk>=0.3.4,<0.4`
- `.whl` path in requirements must be absolute (`/app/agents/{app_name}/...`)
- `--build-service-account` required via `--` passthrough

```bash
adk deploy cloud_run \
  --project=$PROJECT --region=us-central1 \
  --app_name=engineer_agent \
  --service_name=ran-engineer-test \
  --a2a \
  --update-env-vars=GOOGLE_GENAI_USE_VERTEXAI=true,AGENT_MODEL=gemini-2.5-flash \
  -- \
  --build-service-account=projects/$PROJECT/serviceAccounts/techm-dev@$PROJECT.iam.gserviceaccount.com \
  engineer_agent/
```

### 4. Verify

```bash
python tests/test_deployed_agents.py
```

### 5. (Optional) Start MCP Toolbox for Spanner

```bash
./start_mcp_toolbox.sh   # Terminal 1: starts MCP Toolbox on port 5000
```

---

## A2A Architecture

```
                   ┌──────────────┐
                   │  A2A Protocol │
                   │ (JSON-RPC)    │
                   └──────┬───────┘
                          │
          ┌───────────────┼───────────────┐
          │               │               │
          ▼               ▼               ▼
 ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
 │ ReflexAgent  │ │EngineerAgent │ │Reflection    │
 │ (LLM triage  │ │(LLM healing  │ │Agent (LLM    │
 │  via GNN)    │ │ plan gen)    │ │ verification)│
 └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
        │                │                │
        ▼                ▼                ▼
 ┌─────────────────────────────────────────────────┐
 │      Cloud Run (3 independent services)          │
 │   A2A: POST /a2a/{app_name}                      │
 │   ADK API: POST /run                             │
 └─────────────────────────────────────────────────┘
```

Each agent is a standalone Cloud Run service. Agents call each other via A2A JSON-RPC (`POST /a2a/{app_name}`) using these methods:

| Method | Purpose |
|--------|---------|
| `SendMessage` | Send a task/message to the agent |
| `GetTask` | Get task status and artifacts |
| `ListTasks` | List all tasks for a session |
| `CancelTask` | Cancel a running task |

---

## Deployed Agents

| Agent | Service Name | App Name | URL |
|-------|-------------|----------|-----|
| ReflexAgent | `ran-reflex-test` | `reflex_agent` | `https://ran-reflex-test-761300295499.us-central1.run.app` |
| EngineerAgent | `ran-engineer-test` | `engineer_agent` | `https://ran-engineer-test-761300295499.us-central1.run.app` |
| ReflectionAgent | `ran-reflection-test` | `reflection_agent` | `https://ran-reflection-test-761300295499.us-central1.run.app` |

All deployed in `us-central1`, project `poc-z-in2300756`. Authentication required (identity tokens).

### A2A Agent Card Endpoints

```
GET /a2a/reflex_agent/.well-known/agent-card.json
GET /a2a/engineer_agent/.well-known/agent-card.json
GET /a2a/reflection_agent/.well-known/agent-card.json
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
| `MAX_PIPELINE_LOOPS` | `30` | Max retry iterations (ReflectionAgent) |
| `USE_CASE_ID` | `uc1` | Fault scenario (`uc1` or `uc2`) |

---

## Testing

| Command | What it tests | LLM calls? | Time |
|---------|---------------|------------|------|
| `python tests/test.py` | All 6 pipeline steps (pure logic) | No | <1s |
| `python tests/test_live.py` | Full pipeline with Gemini | Yes | ~30s |
| `USE_CASE_ID=uc2 python tests/test.py` | Core fault scenario | No | <1s |
| `python tests/test_deployed_agents.py` | Health check all 3 Cloud Run services | No | ~5s |
| `python tests/test_invoke_all.py` | Invoke all 3 via ADK API | Yes | ~30s |

Tests produce detailed logs in `logs/` with JSON summaries.

---

## Architecture Rules

- **A2A communication**: Agents communicate via JSON-RPC (`POST /a2a/{app_name}`). No orchestrator, no Pub/Sub.
- **Event bus = session state** — `state["event_bus"]` list with `latest_{event_type}` keys.
- **`events.py`** is the lowest-level module — must NEVER import from agents.
- **ReflectionAgent calls tools directly** (no LLM loop) to avoid timeout hangs.
- **Mocks → Production**: swap `providers/*_provider.py` with real API clients. No agent logic changes.
- **Import isolation**: `ran_healing_shared` is a pip-installable package. Agents import from it — no `sys.path.insert` needed.
- **Agent `agent.json`**: uses v0.3 Pydantic AgentCard schema with REQUIRED `url` field. Without this, A2A endpoints return 404.
- **`a2a-sdk` pin**: must be `>=0.3.4,<0.4`. v1.0.x lacks `a2a.server.apps` required by ADK 1.33.0.

---

## Mocks & Production Swaps

| Mock | Real Implementation | Swap Location |
|------|-------------------|---------------|
| `detective_provider.py` | Ericsson A2A Detective API | `ran_healing_shared/providers/` |
| `execution_provider.py` | Ericsson EIC API | `ran_healing_shared/providers/` |
| `gnn_inference_provider.py` | Real GNN Inference | `ran_healing_shared/` |
| `failure_injection_ms.py` | OSS/NFV event stream | `ran_healing_shared/` |
| Spanner MCP Toolbox | ReflexAgent direct Spanner | MCP Toolbox or `call_gnn_engine` fallback |
