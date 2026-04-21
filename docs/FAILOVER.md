# FinHouse — Manual Failover Playbook

This document describes what to do manually when one of your servers
fails or runs out of VRAM. You decide what to move based on the real
situation; these are the mechanical steps for each move.

## Baseline topology (assumed)

- **ip1** runs: `system`, `models` (embed+rerank)
- **ip2** runs: `ollama`, `pipeline` (NiFi+Spark+ClickHouse)

---

## Scenario A — ip1 GPU dies, embed+rerank need to go elsewhere

Embed/rerank use ~4 GB VRAM. Two options:

### Option A1 — move embed/rerank to ip2, drop pipeline GPU usage

Pipeline stack doesn't need GPU. Free-up on ip2 by continuing to run
pipeline (CPU-only) and adding `models` stack.

**On ip2:**

```bash
ssh <SSH_USER>@<ip2>
# password prompted: <SSH_PASSWORD>

cd /path/to/FinHouse

# Verify current VRAM usage — ollama is probably using ~10 GB of your 15 GB
nvidia-smi

# Bring up models stack alongside existing ollama + pipeline
./start.sh models

# Wait for startup
docker logs -f finhouse-bge-m3 --tail 50   # wait for "application startup complete"
docker logs -f finhouse-reranker --tail 50

# Verify
curl http://localhost:28081/health
curl http://localhost:28082/health
```

**On ip1:**

```bash
ssh <SSH_USER>@<ip1>
cd /path/to/FinHouse

# Edit .env — point embed/rerank at ip2
nano .env
#   EMBED_HOST=http://<ip2>:28081
#   RERANK_HOST=http://<ip2>:28082

# Stop local models stack (ip1 GPU is dead anyway)
./start.sh models down

# Restart API to pick up new EMBED_HOST/RERANK_HOST
docker restart finhouse-api

# Verify
docker logs finhouse-api --tail 30 | grep -iE "embed|rerank"
```

### Option A2 — switch to managed API (if ip2 can't absorb more VRAM)

**On ip1:**

```bash
ssh <SSH_USER>@<ip1>
cd /path/to/FinHouse

nano .env
#   EMBED_MODE=backup
#   RERANK_MODE=backup
#   EMBED_API_URL=https://mkp-api.fptcloud.com/v1
#   EMBED_API_KEY=<your-key>
#   EMBED_API_MODEL=Vietnamese_Embedding
#   RERANK_API_URL=https://mkp-api.fptcloud.com/v1
#   RERANK_API_KEY=<your-key>
#   RERANK_API_MODEL=bge-reranker-v2-m3

./start.sh models down
docker restart finhouse-api

# Test end-to-end by sending a chat
curl -X POST http://localhost:18000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"user_name":"you","user_password":"your_pass"}'
# → grab the access_token, create session, send a message via the UI

# Watch logs to confirm API fallback
docker logs -f finhouse-api | grep -iE "embed|api|switch"
# Should see POSTs to mkp-api.fptcloud.com
```

---

## Scenario B — ip2 GPU dies, LLM needs to go elsewhere

### Option B1 — move Ollama to ip1, remove local embed

If ip1 GPU is healthy but busy with embed/rerank, you need to shuffle:
stop embed/rerank on ip1 to free VRAM, run Ollama there, use managed API
for embed/rerank.

**On ip1:**

```bash
ssh <SSH_USER>@<ip1>
cd /path/to/FinHouse

# Free ip1 GPU for Ollama
./start.sh models down

# Pull Ollama stack up
./start.sh ollama

# Pre-pull the LLM (first-time — takes ~5 min)
docker exec finhouse-ollama ollama pull qwen2.5:14b

# Edit .env
nano .env
#   OLLAMA_HOST=http://finhouse-ollama:11434    (local now)
#   EMBED_MODE=backup
#   RERANK_MODE=backup
#   EMBED_API_URL=https://mkp-api.fptcloud.com/v1
#   EMBED_API_KEY=<your-key>
#   RERANK_API_URL=https://mkp-api.fptcloud.com/v1
#   RERANK_API_KEY=<your-key>

# Restart API
docker restart finhouse-api
```

**On ip2:**

```bash
ssh <SSH_USER>@<ip2>
cd /path/to/FinHouse

# Stop broken LLM
./start.sh ollama down

# Pipeline keeps running (CPU only, doesn't need GPU)
```

### Option B2 — LLM via hosted provider

If neither host has GPU, point `OLLAMA_HOST` at a hosted Ollama-compatible endpoint:

```bash
ssh <SSH_USER>@<ip1>
cd /path/to/FinHouse

nano .env
#   OLLAMA_HOST=https://ollama-cloud.example.com
#   EMBED_MODE=backup
#   RERANK_MODE=backup
#   EMBED_API_URL=...
#   EMBED_API_KEY=...

./start.sh models down
# (no need to run ollama stack here)

docker restart finhouse-api
```

---

## Scenario C — ip2 completely down (pipeline + LLM lost)

If ip2 is fully unreachable, you need to move OR accept loss:

**Move to ip1 (minimum viable):**

```bash
ssh <SSH_USER>@<ip1>
cd /path/to/FinHouse

# Keep system stack running. Stop models if we need the GPU for ollama.
./start.sh models down

# Run ollama locally
./start.sh ollama
docker exec finhouse-ollama ollama pull qwen2.5:14b

# Edit .env
nano .env
#   OLLAMA_HOST=http://finhouse-ollama:11434
#   EMBED_MODE=backup        # managed API for embed
#   RERANK_MODE=backup
#   CLICKHOUSE_HOST=         # empty → disable database_query tool
#   EMBED_API_URL=...
#   EMBED_API_KEY=...
#   RERANK_API_URL=...
#   RERANK_API_KEY=...

docker restart finhouse-api
```

**Accept loss of pipeline (database_query tool disabled):**

In `.env`, leave `CLICKHOUSE_HOST=` empty. The `database_query` tool will
report as disabled; the LLM just won't offer it. Chat still works for
RAG-based questions over your documents.

---

## Scenario D — ip1 completely down (app + embed lost)

ip1 has the app + DB + RAG index. Without it, the system is offline.

**To restore**, you need to stand up the `system` stack somewhere else:

**On ip2 (or a new ip3):**

```bash
ssh <SSH_USER>@<ip2>
cd /path/to/FinHouse

# Backup before changes
./scripts/backup-volumes.sh --no-models

# Pull in the ip1 volume backup (if you have one)
scp <SSH_USER>@<ip1>:/path/to/FinHouse/finhouse-backup-*.tar.gz .
./scripts/restore-volumes.sh finhouse-backup-*.tar.gz --force

# Edit .env for new topology
nano .env
#   OLLAMA_HOST=http://finhouse-ollama:11434   (now local)
#   EMBED_MODE=backup                           (no GPU left for embed)
#   RERANK_MODE=backup
#   EMBED_API_URL=...  EMBED_API_KEY=...
#   RERANK_API_URL=... RERANK_API_KEY=...
#   CLICKHOUSE_HOST=finhouse-clickhouse         (local — pipeline still here)

# Run system stack alongside ollama + pipeline
./start.sh system

# Update UI URL for end-users: ip2:8501 instead of ip1:8501
```

If you have no recent backup, you lose chat history and RAG embeddings.
Ingested OLAP data survives because ClickHouse lives on ip2 (in this topology).

---

## Scenario E — Temporary VRAM pressure (not a failure)

ip1 or ip2 GPU gets spiky but hasn't failed. Instead of failover, try:

### Reduce local model footprint

```bash
# On ip2 — use a smaller LLM
ssh <SSH_USER>@<ip2>
cd /path/to/FinHouse
nano .env
#   DEFAULT_MODEL=llama3.1:8b     (was qwen2.5:14b, ~6 GB vs ~10 GB)

# Pull if missing
docker exec finhouse-ollama ollama pull llama3.1:8b

# Restart API on ip1 to pick up model default
ssh <SSH_USER>@<ip1> "cd /path/to/FinHouse && docker restart finhouse-api"
```

### Enable auto-fallback for embed/rerank

```bash
# On ip1
nano .env
#   EMBED_MODE=auto        (try local first, fall back to API if failures)
#   RERANK_MODE=auto
#   LOCAL_FAILURE_THRESHOLD=2

docker restart finhouse-api
```

In this mode the API uses local GPU normally and automatically flips to
managed API when local calls fail twice in a row. A cheap safety net that
doesn't require manual intervention.

---

## Scenario F — Rolling maintenance (planned downtime)

### Upgrade ip2 without user-visible downtime

Users are actively chatting. You want to apply kernel updates to ip2.

**Before maintenance:**

```bash
# Step 1: move LLM temporarily to ip1 or managed
ssh <SSH_USER>@<ip1>
cd /path/to/FinHouse

# Option A: use managed API for everything
nano .env
#   OLLAMA_HOST=https://ollama-cloud.example.com
#   (or run local ollama — stop models first to free GPU)

# Option B: run Ollama on ip1 temporarily
./start.sh models down
./start.sh ollama
docker exec finhouse-ollama ollama pull qwen2.5:14b
nano .env
#   OLLAMA_HOST=http://finhouse-ollama:11434

docker restart finhouse-api

# Step 2: stop ip2 services gracefully
ssh <SSH_USER>@<ip2>
cd /path/to/FinHouse
./start.sh ollama down
./start.sh pipeline down

# Now safe to reboot ip2

# Step 3: after ip2 reboot
ssh <SSH_USER>@<ip2>
./start.sh ollama
./start.sh pipeline

# Step 4: revert ip1
ssh <SSH_USER>@<ip1>
./start.sh ollama down
./start.sh models
nano .env
#   OLLAMA_HOST=http://<ip2>:21434
docker restart finhouse-api
```

---

## Recovery checklist

After any scenario, verify:

```bash
# On ip1
docker ps                         # all expected containers running
curl http://localhost:18000/health
curl http://localhost:8501/       # UI reachable

# Chat flow test
# 1. Login via UI
# 2. Send a message → should get response
# 3. Ask "show me all tables" → database_query tool triggers
# 4. Ask "graph those results as bar chart" → visualize tool triggers
# 5. Upload a PDF to a project → RAG ingest works

# Check logs for errors
docker logs finhouse-api --tail 100 | grep -iE "error|fail"
```

## Helpful SSH one-liners

```bash
# Where is my env pointing right now?
ssh <SSH_USER>@<host> "cd /path/to/FinHouse && grep -E '^(OLLAMA_HOST|EMBED_HOST|RERANK_HOST|CLICKHOUSE_HOST|EMBED_MODE|RERANK_MODE)=' .env"

# What containers are up?
ssh <SSH_USER>@<host> "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"

# Tail API logs from a remote host
ssh <SSH_USER>@<ip1> "docker logs -f finhouse-api --tail 100"

# Restart API remotely
ssh <SSH_USER>@<ip1> "docker restart finhouse-api"
```

## The golden rule

**Always edit `.env` BEFORE restarting services.** The API reads `.env`
on container start. If you restart without changing `.env`, the service
comes back with the old topology.

## What not to do

- **Don't** destroy `volumes/postgres` or `volumes/milvus` unless you want
  a full reset. These hold all chat history and RAG indexes.
- **Don't** run `./start.sh all down` on a live production host — it takes
  down everything including DB.
- **Don't** change `CLICKHOUSE_USER` or `CLICKHOUSE_DB` after pipeline is
  initialized — Spark jobs will fail. Wipe `volumes/clickhouse` and restart
  pipeline if you must.
- **Don't** expose `21434`, `28123`, `28081`, `28082` to the public internet.
  These services have no authentication. Firewall them to ip1's private IP only.
