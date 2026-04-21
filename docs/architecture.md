# FinHouse — System Architecture

## Overview

FinHouse is a fully self-hosted, Dockerized RAG-based AI chat platform. All components run as Docker containers orchestrated via Docker Compose. No external API keys are required — LLM inference, embedding, and reranking all run locally through Ollama and dedicated model containers.

---

## High-Level Architecture

The system is organized into four logical tiers:

1. **Presentation Tier** — Streamlit UI served via the Streamlit server (replaces the earlier React/Nginx plan for faster prototyping and native Python integration).
2. **Application Tier** — FastAPI backend handling API routing, authentication, session management, prompt assembly, SSE streaming, and background task orchestration.
3. **Storage Tier** — PostgreSQL (relational state), MinIO (object/file storage), and Milvus (vector database for RAG embeddings).
4. **AI / Intelligence Tier** — Ollama LLM, BGE-M3 embedding model, BGE-M3 reranker, and LangChain/LangGraph agent framework with pluggable tools.

All four tiers are containerized and communicate over a shared Docker bridge network.

---

## Component Breakdown

### 1. Streamlit UI (Frontend)

| Property       | Value                                           |
|----------------|--------------------------------------------------|
| Framework      | Streamlit (Python)                               |
| Container      | `finhouse-ui`                                    |
| Port           | 8501 (default Streamlit)                         |
| Upstream       | FastAPI backend via HTTP/SSE                     |

Streamlit was chosen over a React SPA for several reasons: tight Python ecosystem integration (shared data models with FastAPI), built-in support for streaming output, rapid prototyping of file upload and interactive widgets, and zero JavaScript build tooling. The UI specification in `ui.md` describes the layout and behavior; Streamlit custom components and `st.columns` / `st.sidebar` replicate the sidebar + main area pattern.

Key UI capabilities:
- Sidebar: project navigation, chat history, new chat, search, settings.
- Main area: model selector dropdown, incognito toggle, streaming chat viewport with Markdown rendering, file upload chips, tool toggles.
- All state that needs persistence across page reruns is managed via `st.session_state` and synced to the FastAPI backend.

### 2. FastAPI Backend (Application Server)

| Property       | Value                                            |
|----------------|---------------------------------------------------|
| Framework      | FastAPI (Python 3.11+)                            |
| Container      | `finhouse-api`                                    |
| Port           | 8000                                              |
| Workers        | Uvicorn with multiple workers (configurable)      |

Responsibilities:
- **Authentication**: JWT-based login/register. Guest users (user_id = 0) bypass auth entirely.
- **Session & Project CRUD**: Create, list, rename, delete projects and chat sessions.
- **Prompt Assembly**: Before each LLM call, FastAPI constructs the prompt window from the latest checkpoint, recent summaries, last 6 message events, current RAG context, and the user query.
- **SSE Streaming**: Streams LLM tokens to the Streamlit frontend in real time via Server-Sent Events.
- **File Upload Orchestration**: Receives files, stores them in MinIO, enqueues the ingest job.
- **Background Worker**: Runs async tasks for summary generation (every 3 turns), checkpoint generation (every 3 summaries), and file ingest (chunking → embedding → Milvus indexing).
- **Tool Dispatch**: Routes tool calls from the LangGraph agent to the appropriate tool executor (web search, database query, code execution for visualization).

### 3. Storage Components

#### PostgreSQL

| Property       | Value                                       |
|----------------|----------------------------------------------|
| Container      | `finhouse-postgres`                          |
| Port           | 5432                                         |
| Volume          | Named Docker volume for data persistence    |

Stores all relational state: users, projects, chat sessions, the append-only chat event log, and file metadata. Schema details are in `db.md`.

#### MinIO (S3-Compatible Object Storage)

| Property       | Value                                       |
|----------------|----------------------------------------------|
| Container      | `finhouse-minio`                             |
| Ports          | 9000 (API), 9001 (Console)                   |
| Bucket         | `finhouse-files`                             |

Stores uploaded documents (PDF, MD, TXT, DOCX). Path conventions:
- Normal: `finhouse-files/user_{id}/project_{id}/{file_id}_{filename}`
- Incognito: `finhouse-files/incognito/{session_id}/{file_id}_{filename}`

#### Milvus (Vector Database)

| Property       | Value                                       |
|----------------|----------------------------------------------|
| Container      | `finhouse-milvus` (standalone mode)          |
| Port           | 19530                                        |
| Dependencies   | etcd, MinIO (Milvus internal storage)        |

Stores document chunk embeddings produced by BGE-M3. Collections are partitioned by `project_id` to enable project-scoped RAG retrieval and efficient cleanup of incognito data.

### 4. AI / Intelligence Components

#### Ollama LLM

| Property       | Value                                       |
|----------------|----------------------------------------------|
| Container      | `finhouse-ollama`                            |
| Port           | 11434                                        |
| GPU            | NVIDIA GPU passthrough via Docker `--gpus`   |
| Volume          | Named volume for model weights              |

Runs the primary chat LLM. Models are pulled at first startup or on-demand via the Ollama API.

**Recommended Models for Tool Calling:**

| Model                | Parameters | VRAM Required | Tool Calling | Notes |
|----------------------|-----------|---------------|-------------|-------|
| **Qwen 2.5:14b**    | 14B       | ~10 GB        | Native      | Best balance of quality, speed, and tool support. Strong at structured output, code generation, and following complex system prompts. Recommended as the default model. |
| **Qwen 2.5:32b**    | 32B       | ~20 GB        | Native      | Higher quality reasoning and longer context. Requires a 24 GB+ GPU. Use when accuracy matters more than latency. |
| **Qwen 2.5:7b**     | 7B        | ~5 GB         | Native      | Lightweight fallback for machines with limited VRAM. Adequate for simple Q&A and basic tool use. |
| **Mistral-Small:24b** | 24B     | ~16 GB        | Native      | Strong multilingual and tool-calling support. Good alternative to Qwen 2.5:32b. |
| **Llama 3.1:8b**    | 8B        | ~6 GB         | Native      | Meta's open model with native tool calling. Good general quality but weaker at code generation than Qwen. |
| **QwQ:32b**          | 32B       | ~20 GB        | Via prompt   | Specialized reasoning model. Excellent for complex multi-step analytical queries. Tool calling via structured prompting rather than native function calling. |

**Model selection guidance:**
- For **chart/graph generation via Python code**, Qwen 2.5 models are strongly recommended — they produce clean, executable matplotlib/plotly/seaborn code with minimal hallucination in import statements and API usage.
- For **database query tool** (SQL generation), Qwen 2.5:14b+ and Mistral-Small both perform well. Qwen tends to produce more precise column references.
- For **web search tool**, any model with native tool calling works. The LangGraph agent handles the tool dispatch; the model just needs to emit the correct function call format.
- **Default recommendation**: Start with **Qwen 2.5:14b** — it covers all three tools well, runs on a single consumer GPU (RTX 3090/4090), and has excellent instruction-following for the prompt assembly format FinHouse uses.

#### BGE-M3 (Embedding Model)

| Property       | Value                                       |
|----------------|----------------------------------------------|
| Container      | `finhouse-bge-m3`                            |
| Port           | 8081 (or embedded in API)                    |
| Model          | `BAAI/bge-m3`                                |

Generates dense + sparse embeddings for document chunks during the ingest pipeline. Supports multi-granularity retrieval (dense, sparse, multi-vector). Can run on CPU or GPU.

#### BGE-M3 Reranker

| Property       | Value                                       |
|----------------|----------------------------------------------|
| Container      | `finhouse-reranker`                          |
| Port           | 8082 (or embedded in API)                    |
| Model          | `BAAI/bge-reranker-v2-m3`                    |

Reranks the top-K candidate chunks retrieved by BGE-M3 to improve precision before injecting into the LLM prompt. Called by FastAPI during the RAG retrieval step.

#### LangChain / LangGraph (Agent Framework)

| Property       | Value                                       |
|----------------|----------------------------------------------|
| Runtime        | Embedded in the FastAPI container            |
| Framework      | LangChain + LangGraph (Python)               |

The agentic layer that connects the Ollama LLM to external tools. LangGraph manages the agent's state machine: receive user query → decide if tools are needed → call tool(s) → synthesize final answer.

**Registered Tools:**

| Tool Name           | Description                                                                                           |
|----------------------|-------------------------------------------------------------------------------------------------------|
| `web_search`         | Queries SearXNG (self-hosted) or DuckDuckGo for real-time web results. Returns top-N snippets.       |
| `database_query`     | Generates SQL from natural language, executes it against the OLAP database, and returns tabular results. |
| `visualize`          | Generates Python code (matplotlib, plotly, or seaborn) to produce charts/graphs from query results. The code is executed in a sandboxed environment and the resulting image or interactive HTML is returned to the chat. |

**Visualization Tool — Detail:**

When the user asks about trends, distributions, comparisons, or any data that benefits from a visual representation, the agent invokes the `visualize` tool. The workflow:

1. The LLM first calls `database_query` to fetch the raw data.
2. The LLM then calls `visualize` with a natural language description of the desired chart.
3. The `visualize` tool executor prompts the LLM to generate Python code using matplotlib, plotly, or seaborn.
4. The generated code is executed in a sandboxed Python subprocess with access to the query result (passed as a pandas DataFrame).
5. The output (PNG image or Plotly HTML) is saved to MinIO and returned as an inline image or interactive widget in the chat.

This approach leverages the LLM's code generation strength (especially Qwen 2.5) to produce flexible, query-specific visualizations without hardcoded chart templates.

---

## Docker Compose Services

All services are defined in a single `docker-compose.yml`:

| Service              | Image / Build          | Depends On                         | GPU  |
|----------------------|------------------------|-------------------------------------|------|
| `finhouse-ui`        | Build from `./ui`      | `finhouse-api`                     | No   |
| `finhouse-api`       | Build from `./api`     | `finhouse-postgres`, `finhouse-milvus`, `finhouse-minio`, `finhouse-ollama` | No   |
| `finhouse-postgres`  | `postgres:16`          | —                                   | No   |
| `finhouse-minio`     | `minio/minio`          | —                                   | No   |
| `finhouse-milvus`    | `milvusdb/milvus`      | `finhouse-etcd`, `finhouse-minio`  | No   |
| `finhouse-etcd`      | `quay.io/coreos/etcd`  | —                                   | No   |
| `finhouse-ollama`    | `ollama/ollama`        | —                                   | Yes  |
| `finhouse-bge-m3`    | Build from `./embed`   | —                                   | Optional |
| `finhouse-reranker`  | Build from `./rerank`  | —                                   | Optional |

**Network**: All containers share a Docker bridge network (`finhouse-net`). Service discovery uses Docker DNS (container names as hostnames).

**Volumes**:
- `pg-data` → PostgreSQL data directory
- `minio-data` → MinIO storage
- `milvus-data` → Milvus persistent storage
- `ollama-models` → Downloaded Ollama model weights

---

## Security Considerations

- **JWT authentication** with short-lived access tokens and refresh tokens. Guest (user_id = 0) sessions skip auth entirely.
- **MinIO** is configured with internal access keys, not exposed to the public network.
- **Ollama** API is not exposed externally — only accessible from within the Docker network.
- **Sandboxed code execution** for the `visualize` tool runs in an isolated subprocess with restricted filesystem access and no network access, preventing arbitrary code execution attacks from LLM-generated code.
- **SQL injection prevention** in the `database_query` tool: the generated SQL is validated and executed with a read-only database connection against the OLAP database (separate from the application database).

---

## Deployment

1. Clone the repository.
2. Copy `.env.example` to `.env` and configure secrets (PostgreSQL password, MinIO keys, JWT secret).
3. Run `docker compose up -d`.
4. On first startup, Ollama automatically pulls the default model (`qwen2.5:14b`).
5. Access Streamlit UI at `http://localhost:8501`.
6. Access FastAPI docs at `http://localhost:8000/docs`.