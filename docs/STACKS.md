# FinHouse — Stacks Deployment Guide

Four independent Docker Compose stacks. Same codebase on every server,
different `start.sh` command per host depending on what you want that
host to run.

## The four stacks

| Stack | Services | Exposes | Needs GPU | Purpose |
|---|---|---|---|---|
| **system** | API, UI, Postgres, MinIO, Milvus, etcd, SearXNG | 8501 (UI), 18000 (API), 19000/19001 (MinIO), 15432 (PG) | No | Core app + RAG index |
| **ollama** | Ollama LLM + model puller | 21434 | Yes (or CPU + slow) | Chat model |
| **models** | BGE-M3 embed + BGE reranker | 28081, 28082 | Yes (recommended) | RAG embed/rerank |
| **pipeline** | NiFi + Spark + ClickHouse + runner | 8090 (NiFi), 28123 (ClickHouse), 8080 (Spark UI) | No | OLAP data pipeline |

## Default topology (your setup)

```
┌─────────────────────────────────────────┐         ┌─────────────────────────────────────────┐
│  <ip1>  (APP + EMBED GPU)               │         │  <ip2>  (LLM GPU + PIPELINE)            │
│  ─────────────────────────────────────  │         │  ─────────────────────────────────────  │
│  ./start.sh system                      │         │  ./start.sh ollama                       │
│  ./start.sh models                      │         │  ./start.sh pipeline                     │
│                                         │         │                                          │
│  Services:                              │         │  Services:                               │
│    • FastAPI            :18000          │────────→│    • Ollama LLM         :21434           │
│    • Streamlit UI       :8501           │         │    • NiFi (file watcher):8090            │
│    • PostgreSQL         :15432          │         │    • Spark master UI    :8080            │
│    • MinIO              :19000/19001    │         │    • ClickHouse HTTP    :28123           │
│    • Milvus             :19530          │         │    • ClickHouse native  :29000           │
│    • SearXNG            :18080          │         │                                          │
│    • BGE-M3 embed       :28081          │         │  Data:                                   │
│    • BGE reranker       :28082          │         │    • /data/OLAP (source files)           │
│                                         │         │    • /data/checkpoint (manifests)        │
│  GPU usage (on A4000 15.7GB):           │         │                                          │
│    • BGE-M3      ~2.5 GB                │         │  GPU usage:                              │
│    • Reranker    ~1.5 GB                │         │    • qwen2.5:14b ~10 GB                  │
│    • Free        ~11 GB                 │         │    • Free        ~5 GB                   │
└─────────────────────────────────────────┘         └─────────────────────────────────────────┘
          │                                                       │
          │  HTTP to http://<ip2>:21434    (Ollama)                │
          │  HTTP to http://<ip2>:28123    (ClickHouse)            │
          └───────────────────────────────────────────────────────┘
```

### Why this split?

- **GPU balancing**: LLM (~10 GB VRAM) on ip2, embed/rerank (~4 GB) on ip1. Each
  server has headroom.
- **Co-located pipeline**: ClickHouse is queried by the API's `database_query`
  tool. If NiFi/Spark/ClickHouse are on ip2, only the final query hits the
  network — the heavy ingest stays local on ip2.
- **Co-located embed+API**: the API calls embed on every chat turn. Same-host
  loopback is faster than cross-server.

## Quick start

### On ip1

```bash
git clone <your-repo> FinHouse
cd FinHouse
cp .env.example .env
nano .env
# Set:
#   IP1=<ip1-actual>
#   IP2=<ip2-actual>
#   OLLAMA_HOST=http://<ip2>:21434
#   EMBED_HOST=http://finhouse-bge-m3:8081     (local — models stack on ip1)
#   RERANK_HOST=http://finhouse-reranker:8082
#   CLICKHOUSE_HOST=<ip2>                      (remote — pipeline on ip2)
#   Passwords, JWT_SECRET, API keys
```

```bash
chmod +x start.sh
./start.sh system
./start.sh models
```

### On ip2

```bash
git clone <your-repo> FinHouse
cd FinHouse
cp .env.example .env
nano .env
# Set same IP1, IP2, and passwords as ip1.
# OLLAMA_HOST and CLICKHOUSE_HOST can stay at Docker names (local here).
```

```bash
chmod +x start.sh
./start.sh ollama
./start.sh pipeline
```

### Open firewalls

On ip2, allow inbound from ip1:

```bash
sudo ufw allow from <ip1> to any port 21434 proto tcp    # Ollama
sudo ufw allow from <ip1> to any port 28123 proto tcp    # ClickHouse HTTP
sudo ufw allow from <ip1> to any port 8090 proto tcp     # NiFi UI (if you want to manage it from ip1)
```

## Deployment patterns

### Pattern 1 — single host (dev / demo)

Everything on one box with a GPU:

```bash
./start.sh all
```

Uses defaults in `.env` (all services reach each other via Docker network).

### Pattern 2 — default split (recommended)

See "Default topology" above.

### Pattern 3 — three hosts (heavy production)

- ip1 = system (app only)
- ip2 = models (embed + rerank)
- ip3 = ollama + pipeline

```bash
# On ip1
nano .env
#   OLLAMA_HOST=http://<ip3>:21434
#   EMBED_HOST=http://<ip2>:28081
#   RERANK_HOST=http://<ip2>:28082
#   CLICKHOUSE_HOST=<ip3>
./start.sh system
```

```bash
# On ip2
./start.sh models
```

```bash
# On ip3
./start.sh ollama
./start.sh pipeline
```

### Pattern 4 — everything managed, no GPU

```bash
# .env on ip1
EMBED_MODE=backup
RERANK_MODE=backup
OLLAMA_HOST=https://your-llm-provider.example.com
EMBED_API_URL=https://mkp-api.fptcloud.com/v1
EMBED_API_KEY=your-key
RERANK_API_URL=https://mkp-api.fptcloud.com/v1
RERANK_API_KEY=your-key
CLICKHOUSE_HOST=    # leave empty → database_query tool disabled
```

```bash
./start.sh system
```

No GPU needed anywhere. You lose the `database_query` tool unless you host
ClickHouse elsewhere.

## Mode selector — EMBED_MODE / RERANK_MODE

Applies regardless of where the models stack runs. Controls routing inside the API:

```bash
EMBED_MODE=local     # call EMBED_HOST only (error on failure)
EMBED_MODE=backup    # call EMBED_API_URL only (skip local)
EMBED_MODE=auto      # try EMBED_HOST, sticky-switch to API after N failures
```

Caller code (`embed_texts()`, `rerank_chunks()`) is unchanged — internal
dispatcher reads the mode from `.env`.

## Network considerations

### Same host — stacks share network

All stacks declare network `finhouse-net`. First stack creates it, others attach.
Containers across stacks reach each other by name (`finhouse-postgres`, `finhouse-clickhouse`, etc.).

### Split hosts — use public IPs

When stacks run on different hosts:
- Docker network doesn't bridge them
- The app reaches remote services via public IP + port
- Example:
  ```bash
  OLLAMA_HOST=http://<ip2>:21434              # not finhouse-ollama:11434
  CLICKHOUSE_HOST=<ip2>                        # not finhouse-clickhouse
  ```

## Pipeline specifics (NiFi + Spark + ClickHouse)

See `pipeline/nifi/README.md` for full NiFi flow instructions. Summary:

1. Drop CSV/XLSX/.db files into `data/OLAP/` on ip2
2. NiFi ListFile detects them (timestamp tracking, restart-safe)
3. NiFi emits manifest JSON → `data/checkpoint/manifest-YYYY-MM-DDTHH-MM-SS-<filename>.json`
4. `finhouse-pipeline-runner` polls every 10s, submits Spark job
5. Spark reads file, creates/overwrites ClickHouse table with matching name
6. Runner writes `<manifest>.processed` marker on success

The LLM agent's `database_query` tool can then SELECT against `olap.<table_name>`.
Results get piped to `visualize` tool for charts (server-side Matplotlib → MinIO → URL).

### Replacing the prototype schema

`pipeline/clickhouse/init.sql` only runs on FIRST container start. To apply
schema changes later:

```bash
# Edit pipeline/clickhouse/init.sql with your real schema
nano pipeline/clickhouse/init.sql

# On ip2
docker exec -i finhouse-clickhouse \
    clickhouse-client --user finhouse --password "$CLICKHOUSE_PASSWORD" \
    < pipeline/clickhouse/init.sql
```

Or wipe and re-init (destroys existing data):

```bash
./start.sh pipeline down
sudo rm -rf volumes/clickhouse
./start.sh pipeline up
```

## Shared state across hosts

Each host's `./volumes/` is local. They are NOT shared automatically:
- ip1 owns: `volumes/postgres`, `volumes/minio`, `volumes/milvus`, `volumes/etcd`
- ip2 owns: `volumes/ollama`, `volumes/clickhouse`, `volumes/nifi`
- `data/OLAP` and `data/checkpoint` on ip2 only (pipeline host)

`scripts/backup-volumes.sh` backs up THIS host's volumes only. Run it on each host
separately for full backup.

## Verification checklist

After starting both hosts:

```bash
# From ip1
curl http://localhost:18000/health
curl http://<ip2>:21434/api/tags          # Ollama models
curl http://<ip2>:28123/ping              # ClickHouse
curl http://<ip2>:8090/nifi-api/system-diagnostics   # NiFi (may prompt auth)

# From ip1 — check pipeline works end-to-end
ssh <ip2> "echo 'id,name
1,Alice
2,Bob' > /path/to/FinHouse/data/OLAP/test.csv"

# Wait 60s for NiFi + runner
sleep 60

# Query via API
curl http://localhost:18000/health
docker exec finhouse-api python -c "
import asyncio
from tools.database_query import run_sql
result = asyncio.run(run_sql('SELECT * FROM olap.test LIMIT 10'))
print(result)
"
```

## Migrating from the monolithic compose

If you had the old `docker-compose.yml`:

```bash
# Stop old stack
docker compose down

# Pull new code
git pull

# Update .env: add IP1, IP2, CLICKHOUSE_*, NIFI_* sections (see .env.example)

# Start whatever stacks you need
./start.sh system
./start.sh models    # if GPU available
```

Data volumes carry over — no re-ingest needed. The old monolithic file
will be replaced by the 4 stack files.
