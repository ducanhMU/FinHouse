# FinHouse — Portability & Deployment Guide

## Deployment modes

### 1. All-in-one (default)
Everything on one GPU host. Edit `.env`:
```bash
COMPOSE_PROFILES=local-llm,local-embed,local-rerank
```
Run: `docker compose up -d`

### 2. Split: app host + GPU host

**GPU host** (runs models):
```bash
COMPOSE_PROFILES=local-llm,local-embed,local-rerank
# Expose ports on the GPU host's firewall:
#   11434 (Ollama), 8081 (embed), 8082 (rerank)
```

**App host** (runs API, UI, Postgres, Milvus, MinIO):
```bash
COMPOSE_PROFILES=
OLLAMA_HOST=http://gpu-host.example.com:11434
EMBED_HOST=http://gpu-host.example.com:8081
RERANK_HOST=http://gpu-host.example.com:8082
```

### 3. App host + managed APIs (no GPU anywhere)
Fully cloud-based models. Edit `.env`:
```bash
COMPOSE_PROFILES=
# Ollama — pick any compatible cloud (or run your own)
OLLAMA_HOST=https://your-ollama-cloud.example.com

# Managed embed + rerank (FPT Cloud example)
EMBED_API_URL=https://mkp-api.fptcloud.com/v1
EMBED_API_KEY=your-api-key-here
EMBED_API_MODEL=Vietnamese_Embedding

RERANK_API_URL=https://mkp-api.fptcloud.com/v1
RERANK_API_KEY=your-api-key-here
RERANK_API_MODEL=bge-reranker-v2-m3
```
Run: `docker compose up -d`

### 4. Hybrid with auto-fallback
Keep local services, but configure managed APIs as safety net. If local GPU crashes,
the app auto-switches to cloud APIs after `LOCAL_FAILURE_THRESHOLD` consecutive
failures and keeps running.

```bash
COMPOSE_PROFILES=local-llm,local-embed,local-rerank
EMBED_API_URL=https://mkp-api.fptcloud.com/v1
EMBED_API_KEY=...
RERANK_API_URL=https://mkp-api.fptcloud.com/v1
RERANK_API_KEY=...
LOCAL_FAILURE_THRESHOLD=2
```

## Volume backup and restore

### Backup

```bash
# Full backup (includes Ollama/BGE model weights — can be 15+ GB)
./scripts/backup-volumes.sh

# Skip model weights (faster, smaller — you'll re-download on restore)
./scripts/backup-volumes.sh --no-models

# Online mode (no container downtime, but may be inconsistent)
./scripts/backup-volumes.sh --no-stop
```

Outputs `finhouse-backup-YYYYMMDD-HHMMSS.tar.gz` in project root.

The script stops containers during backup for consistency (avoid torn writes),
then restarts. Default downtime is a few minutes depending on volume size.

### Restore on the same host

```bash
./scripts/restore-volumes.sh finhouse-backup-20260417-103000.tar.gz
```

If `./volumes/` already exists and has data, the script refuses unless you pass
`--force`. With `--force` it moves existing data to `volumes.pre-restore.<ts>/`
as a safety copy before extracting.

### Restore on a different host (cross-server transfer)

```bash
# On source host
./scripts/backup-volumes.sh --no-models    # skip model weights
scp finhouse-backup-*.tar.gz user@target-host:/opt/finhouse/

# On target host — make sure you've checked out the same git commit first
cd /opt/finhouse
git checkout $SAME_COMMIT_AS_SOURCE
./scripts/restore-volumes.sh finhouse-backup-*.tar.gz

# Models will re-download on first use (~15 min for LLM + 5 min for embed/rerank)
```

### Volume portability details

| Volume | Portable? | Caveats |
|---|---|---|
| `volumes/postgres` | Yes | Target host must use same Postgres major version (v16) |
| `volumes/minio` | Yes | 100% portable, just files |
| `volumes/etcd` | Yes (with Milvus) | Must be restored together with Milvus |
| `volumes/milvus` | Yes (with etcd) | Must match etcd state exactly |
| `volumes/ollama` | Yes | Model GGUF files, fully portable |

**Key rule: backup Milvus + etcd + MinIO atomically.** Milvus writes metadata
to etcd AND data blobs to MinIO. If you restore one without the others,
the system will be inconsistent. The backup script handles this by stopping
all containers first.

### What the backup contains

- `volumes/` — all persistent data (databases, object storage, vector index, models)
- `.env` — your actual config (INCLUDING SECRETS — protect the archive!)
- `docker-compose.yml` — so target host runs the same service topology

### What it does NOT contain

- The code itself (git clone on target host)
- Docker images (pulled on target host automatically)
- The `./data/` folder (your source documents — back up separately if needed)

### Security note

**The backup archive contains your `.env` file with all secrets.** Treat it as
sensitive data. Don't email it, don't leave it in a public bucket. For long-term
storage, encrypt it:

```bash
# Encrypt on backup
./scripts/backup-volumes.sh
gpg --symmetric --cipher-algo AES256 finhouse-backup-*.tar.gz
rm finhouse-backup-*.tar.gz          # keep only the .gpg

# Decrypt before restore
gpg --decrypt finhouse-backup-*.tar.gz.gpg > finhouse-backup.tar.gz
./scripts/restore-volumes.sh finhouse-backup.tar.gz
```
