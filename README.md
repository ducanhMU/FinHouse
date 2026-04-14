# 🏠 FinHouse

**Self-hosted, RAG-based AI chat platform powered by Ollama.**

All LLM inference, embedding, and reranking run locally — no external API keys required.

---

## Quick Start

### Prerequisites

- Docker & Docker Compose v2+
- NVIDIA GPU + NVIDIA Container Toolkit (for Ollama)
- ~20 GB disk for models, ~16 GB VRAM for Qwen 2.5:14b

### 1. Clone & configure

```bash
git clone <your-repo> finhouse && cd finhouse
cp .env.example .env
# Edit .env — change passwords and JWT secret
```

### 2. Start infrastructure

```bash
docker compose up -d
```

This starts: PostgreSQL, MinIO, etcd, Milvus, Ollama, SearXNG, BGE-M3, Reranker, FastAPI, and Streamlit UI.

### 3. Pull LLM models

```bash
chmod +x scripts/pull-models.sh
./scripts/pull-models.sh
```

This pulls `qwen2.5:14b` and `llama3.1:8b`. Takes 10-30 min depending on bandwidth.

### 4. Access

| Service       | URL                          |
|---------------|------------------------------|
| **Chat UI**   | http://localhost:8501         |
| **API Docs**  | http://localhost:8000/docs    |
| **MinIO**     | http://localhost:9001         |
| **SearXNG**   | http://localhost:8080         |

---

## Architecture

```
User → Streamlit UI (:8501) → FastAPI (:8000) → Ollama LLM (:11434)
                                  ↕                    ↕
                            PostgreSQL            LangGraph Agent
                            MinIO                      ↕
                            Milvus ← BGE-M3      Tools: web_search
                                     Reranker          database_query (soon)
                                                       visualize (soon)
```

All services run in Docker containers on a shared bridge network.

## Models

| Model           | VRAM  | Tool Calling | Best For                    |
|-----------------|-------|--------------|-----------------------------|
| Qwen 2.5:14b   | ~10GB | ✅ Native    | Default — best all-round    |
| Llama 3.1:8b   | ~6GB  | ✅ Native    | Lightweight alternative     |

Pull additional models: `docker exec finhouse-ollama ollama pull <model>`

## Tools

| Tool            | Status      | Description                              |
|-----------------|-------------|------------------------------------------|
| Web Search      | ✅ Ready    | SearXNG-powered internet search          |
| Database Query  | 🚧 Planned | Natural language → SQL against OLAP DB   |
| Visualize       | 🚧 Planned | LLM-generated charts via Python          |

## Project Structure

```
finhouse/
├── docker-compose.yml
├── .env.example
├── api/                    # FastAPI backend
│   ├── Dockerfile
│   ├── main.py             # App entry point + health check
│   ├── config.py           # Environment settings
│   ├── database.py         # Async SQLAlchemy
│   ├── models.py           # ORM models
│   ├── routers/
│   │   ├── auth.py         # Register / login / JWT
│   │   ├── projects.py     # Project CRUD
│   │   ├── sessions.py     # Session CRUD
│   │   ├── chat.py         # Message send / stream / events
│   │   └── files.py        # File upload / list / delete
│   ├── services/
│   │   └── ollama.py       # Ollama HTTP client
│   ├── tools/
│   │   └── web_search.py   # SearXNG search tool
│   └── db/
│       └── init.sql        # Schema + seeds
├── ui/                     # Streamlit frontend
│   ├── Dockerfile
│   ├── app.py              # Main Streamlit app
│   ├── api_client.py       # Backend API helper
│   └── .streamlit/
│       └── config.toml     # Theme (dark indigo)
├── embed/                  # BGE-M3 embedding service
│   ├── Dockerfile
│   └── main.py
├── rerank/                 # BGE-M3 reranker service
│   ├── Dockerfile
│   └── main.py
├── searxng/                # SearXNG config
│   └── settings.yml
└── scripts/
    └── pull-models.sh      # Model download script
```

## Development

Rebuild a single service:
```bash
docker compose up -d --build finhouse-api
```

View logs:
```bash
docker compose logs -f finhouse-api
docker compose logs -f finhouse-ui
```

Reset everything:
```bash
docker compose down -v   # removes volumes too
```
