# AGENTS.md — ano_moonshot_2026

## What this repo is

Multi-version POC of a **RAN Self-Healing** agentic pipeline built on Google ADK. Four project variants (base, v1, v2, v6), plus shared TMF OpenAPI specs. Each variant is self-contained in its own directory.

## Project structure

```
RAN Healing ADK - eventdriven/          # Base event-driven (no ADK UI)
RAN Healing ADK - eventdriven_v1/       # + adk web UI + SequentialAgent orchestrator
RAN Healing ADK - eventdriven_v2/       # + retry logic, service layer, Dockerfile, healing_knowledge_base.py
RAN Healing ADK - eventdriven_v6/
  └── RAN Healing ADK - eventdriven_v6/ # Most evolved: MCP toolbox, Reflex/Engineer/Reflection agents, providers/
TMF-APIS/                               # TMF API specs (628, 640, 641, 642, 688, 702, 915, 921)
```

## Pipeline flow (v6, most current)

```
FailureInjectionMS → ReflexAgent (LLM) → GNN → DetectiveAgent (mock)
  → EngineerAgent (LLM) → ExecutorAgent (mock) → ReflectionAgent (direct tools)
  → RESOLVED or RETRIGGER (max 30 loops)
```

## Commands

```bash
pip install -r requirements.txt          # install in any variant dir
python main.py                           # run pipeline
USE_CASE_ID=uc1 python main.py           # v6: pick use case (uc1=antenna tilt, uc2=HSS failover)
python test.py                           # v6: run pipeline WITHOUT LLM calls (instant, pure Python)
adk web                                  # v1+: launch ADK dev UI at localhost:8000
./start_mcp_toolbox.sh                   # v6 only: start MCP Toolbox for Spanner (Terminal 1)
```

## Environment (Vertex AI / GCP deploy)

```bash
export GOOGLE_CLOUD_PROJECT=your-project-id
export GOOGLE_GENAI_USE_VERTEXAI=TRUE
export GOOGLE_CLOUD_LOCATION=us-central1
gcloud auth application-default login
```

Cloud Run: `adk deploy cloud_run --project=$PROJECT --region=us-central1 --service-name=ran-healing-adk --with-ui`
Agent Engine: `adk deploy agent_engine --project=$PROJECT --region=us-central1`

## Architecture rules

- **Event bus = session state** (`state["event_bus"]` list, `latest_{event_type}` key). No Pub/Sub in local mode.
- **`events.py`** is the lowest-level shared module — must NEVER import from `app.agents`, `app.orchestrator`, or `app.config`.
- **Orchestrator** (`root_agent.py`) is a custom `BaseAgent` subclass with deterministic event routing (if/elif on event type). Sub-agents that need LLM are run via `agent.run_async(ctx)`; external mocks and deterministic steps call tools directly.
- **ReflectionAgent** (v6) calls tools directly (no LLM) — using it as `LlmAgent` caused timeout hangs.
- **Retry** (v2+): ValidationAgent re-publishes `gnn.anomaly.detected` on failure; main loop handles it automatically (max 30 iterations default, configurable via `MAX_PIPELINE_LOOPS` env var).

## Mocks → production swaps

Swap `*_mock_output.py` / `providers/*_provider.py` with real API clients. Each mock has a corresponding `mock_api.py` or `*_service.py` that calls it. No agent logic changes needed.

## Codegen / artifacts

- No CI, no lint, no formatter, no test framework (use `python test.py` in v6 for no-LLM verification).
- `adk web` requires Google ADK CLI installed separately.
- v6 hardcodes `os.environ["AGENT_MODEL"] = "gemini-2.5-flash"` in `main.py`.
- Import path fix (`sys.path.insert`) needed in v2+ main.py for Cloud Run.
