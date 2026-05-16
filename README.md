# 🏠 FinHouse

**Self-hosted, multi-agent RAG platform for Vietnamese corporate-finance Q&A.**

FinHouse answers questions about Vietnamese listed companies (financials,
ratios, sectors, macro) by combining a document RAG layer over uploaded
reports with live tool agents (web search, OLAP database, charting). All
core inference — LLM, embedding, reranking — can run **locally on Ollama**
with no external API keys; cloud providers are optional fallbacks.

---

## Quick Start

### Prerequisites

- Docker & Docker Compose v2+
- NVIDIA GPU + NVIDIA Container Toolkit (for Ollama) — ~16 GB VRAM for `qwen2.5:14b`
- ~25 GB disk for models

### 1. Clone & configure

```bash
git clone <your-repo> finhouse && cd finhouse
cp .env.example .env
# Edit .env — change DB passwords + JWT secret. Optionally set the
# per-agent LLM env vars (see "Multi-LLM routing" below).
```

### 2. Start the stack

```bash
docker compose up -d
```

Brings up: PostgreSQL, MinIO, etcd, Milvus, Ollama, SearXNG, BGE-M3
embedder, BGE reranker, the FastAPI backend and the Streamlit UI. A
one-shot model-puller container pulls the default models on first run.

### 3. Access

| Service          | URL                          |
|------------------|------------------------------|
| **Chat UI**      | http://localhost:8501        |
| **API + Swagger**| http://localhost:18000/docs  |
| **MinIO console**| http://localhost:19001       |
| **SearXNG**      | http://localhost:18080       |
| **Milvus**       | localhost:19530 (gRPC)       |
| **Ollama**       | http://localhost:21434       |

> Host ports are remapped (`1`/`2`-prefixed) to avoid collisions; the
> in-container ports are the conventional ones. See `docker-compose.yml`.

---

## Architecture

FinHouse runs one user turn through a **LangGraph multi-ReAct topology**:

```
                       ┌─ needs clarification ──────────────────────────┐
START → rewriter ──────┤                                                 ▼
                       └─ else (parallel fan-out) ──┬─ rag ──────────► collector → END
                                                    └─ orchestrator → dispatcher ┘
```

- **rewriter** — a ReAct agent. Resolves the question to a self-contained
  form, calls `lookup_company` against the OLAP DB to verify tickers/names,
  detects scope (company / sector / macro / general), applies sensible
  defaults (e.g. latest fiscal year), and emits **HyDE hypothetical
  passages** for multi-query retrieval. If the entity can't be resolved it
  asks for clarification with clickable paraphrase **suggestion chips**.

- **rag** — a 4-stage sub-agent that produces its **own grounded answer**:
  1. *Retriever* — HyDE multi-query embed → hybrid (dense+sparse) search
     in Milvus → cross-encoder rerank → top-K chunks.
  2. *Evaluator* — one JSON LLM call: `sufficient | partial | insufficient`
     plus which chunks are actually useful.
  3. *Web fallback* — only when partial/insufficient: one SearXNG query to
     supplement (not replace) the document context.
  4. *Generator* — a focused Vietnamese answer citing chunks as `[n]`.

- **orchestrator → dispatcher** — the orchestrator plans which tool agents
  to run; the dispatcher runs them **in parallel**, each a ReAct agent over
  one tool type (`web_search`, `database`, `visualize`).

- **collector** — synthesises the final answer from the RAG draft, the raw
  chunks, and every tool agent's findings, then **streams** it token by
  token. Tool agents never block the chat — unmet needs are woven into the
  answer as a follow-up question.

Graph state is Pydantic-typed; parallel branches write disjoint scalars or
`operator.add` lists, so the fan-in at `collector` is conflict-free.

### Streaming protocol (SSE)

The chat endpoint streams typed events the UI renders live:
`query_rewrite`, `clarification` (+ suggestion chips), `rag_sources`
(+ evaluator/web-fallback meta), `orchestrator_plan`, `tool_start`,
`tool_end`, `reasoning`, `token`, `final_answer`, `error`, `done`. The UI
is hardened against interrupted streams (abandoned-stream guard cancels
orphaned backend work; truncated turns are flagged, not silently saved).

### Multi-LLM routing

Every agent resolves its model via a per-agent env var with a provider
**fallback chain** (`ollama` / `dashscope` / `gemini` / `openai`). Empty →
the session model on Ollama. Knobs:
`REWRITER_AGENT_LLM`, `RAG_AGENT_LLM`, `ORCH_AGENT_LLM`, `WEB_AGENT_LLM`,
`DB_AGENT_LLM`, `VIS_AGENT_LLM`, `COLLECTOR_AGENT_LLM` (+ matching
`*_THINKING` flags for reasoning models). See `api/config.py`.

## Tools

| Tool            | Status   | Description                                   |
|-----------------|----------|-----------------------------------------------|
| Web Search      | ✅ Ready | SearXNG internet search (also RAG fallback)   |
| Database Query  | ✅ Ready | NL → SQL over the ClickHouse OLAP warehouse   |
| Visualize       | ✅ Ready | LLM-generated charts                          |
| Market Data     | ✅ Ready | Price / market lookups                        |
| URL Fetch       | ✅ Ready | Fetch & extract a referenced page             |
| Wikipedia       | ✅ Ready | Encyclopaedic context                         |

## Data pipeline (OLAP)

Raw market/financial CSVs are ingested into a ClickHouse OLAP warehouse
via a NiFi → Spark → Airflow pipeline (`pipeline/`), which the `database`
tool queries through `lookup_company` / `list_tables` / `describe_table`
/ SQL.

## Evaluation harness

`evaluation/` is a benchmark runner that scores the pipeline at three
layers (RAG-only, per-agent, end-to-end). All seven nodes (rewriter,
orchestrator, rag, db, web, visualize, collector) emit a standardized
`component_logs` record — including per-call token `usage` and the
test's `category` — only when a benchmark run sets `state.bench`;
production traffic is a no-op, zero overhead. `evaluation/token_stats.py`
rolls the per-call usage up into `tokens.json` (min/max/avg/sum by
component, whole-turn, and per question category); `visualize_chart.py
--tokens` renders it. See [benchmark.md](benchmark.md).

## Models

| Model         | VRAM  | Tool calling | Notes                     |
|---------------|-------|--------------|---------------------------|
| Qwen 2.5:14b  | ~10GB | ✅ Native    | Default, best all-round   |
| Llama 3.1:8b  | ~6GB  | ✅ Native    | Lightweight alternative   |

Pull more: `docker exec finhouse-ollama ollama pull <model>`

## Project structure

```
finhouse/
├── docker-compose.yml
├── .env.example
├── api/                        # FastAPI backend
│   ├── main.py                 # App entry + /health, /models, /agents
│   ├── config.py               # Settings (per-agent LLM routing, RAG flags)
│   ├── database.py  models.py
│   ├── routers/                # auth, projects, sessions, chat, files
│   ├── graph/                  # LangGraph multi-agent runtime
│   │   ├── runtime.py          # Topology / graph compile
│   │   ├── state.py            # Pydantic graph state + reducers
│   │   ├── llm_router.py       # Per-agent model + fallback chain
│   │   ├── sse.py              # Streaming event helpers
│   │   ├── react_agent.py      # Generic ReAct loop
│   │   ├── logging_helper.py   # Benchmark component logs (no-op in prod)
│   │   └── nodes/              # rewriter, rag, orchestrator,
│   │                           #   tool_agents (dispatcher), collector
│   ├── prompts/                # *.md prompts loaded at runtime
│   ├── services/               # ollama, ingest (RAG), rewriter, scanner
│   └── tools/                  # web_search, database_query, visualize,
│                               #   market_data, url_fetch, wikipedia
├── ui/                         # Streamlit frontend (streaming chat)
├── embed/                      # BGE-M3 embedding service
├── rerank/                     # BGE reranker service
├── pipeline/                   # NiFi + Spark + Airflow → ClickHouse OLAP
├── evaluation/                 # 3-layer benchmark harness
│   ├── runner.py               # Async runner + per-layer aggregation
│   ├── token_stats.py          # Token roll-up → tokens.json
│   └── visualize_chart.py      # bar / radar / compare + --tokens
├── searxng/                    # SearXNG config
└── scripts/                    # model pull / migration helpers
```

## Development

```bash
# Rebuild one service
docker compose up -d --build finhouse-api

# Logs
docker compose logs -f finhouse-api
docker compose logs -f finhouse-ui

# Sanity-check the graph builds inside the runtime
docker compose run --rm finhouse-api \
  python -c "from graph.runtime import get_graph; get_graph(); print('GRAPH OK')"

# Reset everything (including volumes)
docker compose down -v
```
