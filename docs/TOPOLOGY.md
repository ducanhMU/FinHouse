# FinHouse — Primary / Failover Deployment

Two-server setup with one-command mode switching.

## Topology

### PRIMARY mode (normal operation)

```
┌─────────────────────────────┐         ┌─────────────────────────────┐
│  <ip1>  (APP HOST)          │         │  <ip2>  (MODEL HOST)        │
│  ──────────────────         │         │  ──────────────────         │
│  • FastAPI + Streamlit      │ ──POST─→│  • BGE-M3 embed   :28081   │
│  • PostgreSQL, MinIO        │ ──POST─→│  • BGE reranker   :28082   │
│  • Milvus + etcd            │         │                             │
│  • Ollama LLM   :21434      │         │  (GPU: ~3.5 GB used)        │
│    (GPU: ~10 GB used)       │         │                             │
└─────────────────────────────┘         └─────────────────────────────┘
```

### FAILOVER mode (when `<ip1>` GPU dies or overloads)

```
┌─────────────────────────────┐         ┌─────────────────────────────┐
│  <ip1>  (APP HOST)          │         │  <ip2>  (MODEL HOST)        │
│  ──────────────────         │         │  ──────────────────         │
│  • FastAPI + Streamlit      │ ──POST─→│  • Ollama LLM     :21434   │
│  • PostgreSQL, MinIO        │         │    (GPU: ~10 GB used)       │
│  • Milvus + etcd            │         │                             │
│  • Ollama NOT running       │         │  • embed NOT running        │
│                             │         │  • rerank NOT running       │
└──────────┬──────────────────┘         └─────────────────────────────┘
           │
           │ embed + rerank via HTTPS
           ▼
┌─────────────────────────────────────────────────────────────────────┐
│   Managed API (FPT Cloud)                                            │
│   • /v1/embeddings (Vietnamese_Embedding, 1024-dim)                  │
│   • /v1/rerank (bge-reranker-v2-m3)                                  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## One-time setup

### On `<ip1>` (app host)

```bash
git clone <your-repo> FinHouse
cd FinHouse

# Generate .env.local with your specifics
./scripts/switch-mode.sh primary
# ^ This creates .env.local. Edit it:
nano .env.local
```

Set in `.env.local`:

```bash
IP1=<ip1>                                   # e.g. 10.0.0.10
IP2=<ip2>                                   # e.g. 10.0.0.20

POSTGRES_PASSWORD=<strong-password>
MINIO_ROOT_PASSWORD=<strong-password>
JWT_SECRET=<32+ character random string>

# Get these from FPT Cloud console (or your managed API provider)
EMBED_API_URL=https://mkp-api.fptcloud.com/v1
EMBED_API_KEY=<your-api-key>
RERANK_API_URL=https://mkp-api.fptcloud.com/v1
RERANK_API_KEY=<your-api-key>
```

Then run `./scripts/switch-mode.sh primary` again to generate `.env` from these values.

```bash
docker compose up -d
```

### On `<ip2>` (model host)

```bash
git clone <your-repo> FinHouse
cd FinHouse

./scripts/switch-mode.sh model-primary
# Also creates .env.local — edit the same way as ip1
nano .env.local
# Set at least IP1, IP2, and any secrets you want to match ip1's

./scripts/switch-mode.sh model-primary
docker compose up -d
```

Open ports on `<ip2>` firewall for `<ip1>` traffic:

```bash
sudo ufw allow from <ip1> to any port 28081 proto tcp
sudo ufw allow from <ip1> to any port 28082 proto tcp
sudo ufw allow from <ip1> to any port 21434 proto tcp   # for failover mode
```

Verify from `<ip1>`:

```bash
curl http://<ip2>:28081/health
# {"status":"ok","model":"BAAI/bge-m3","device":"cuda"}
curl http://<ip2>:28082/health
```

You're in PRIMARY mode. System runs normally.

---

## Switching to FAILOVER (when ip1 GPU dies)

Two commands on two hosts:

### Step 1 — On `<ip2>` (tell it to take over LLM)

```bash
cd /path/to/FinHouse
./scripts/switch-mode.sh model-failover
docker compose down
docker compose up -d

# Pre-pull the LLM on ip2 (only needed first time)
docker exec finhouse-ollama ollama pull qwen2.5:14b
```

Wait ~5 min (model loading into VRAM). Verify:

```bash
curl http://<ip2>:21434/api/tags
# Should list qwen2.5:14b
```

### Step 2 — On `<ip1>` (flip to failover config)

```bash
cd /path/to/FinHouse
./scripts/switch-mode.sh failover
docker compose down
docker compose up -d
```

App is now running with:
- Ollama served from `<ip2>`
- embed + rerank from FPT Cloud managed API

Verify end-to-end:

```bash
# Send a test chat
curl -X POST http://localhost:18000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"user_name":"test","user_password":"password123"}'

# Then login, create a session, send a message via the UI at :8501
```

Watch logs on `<ip1>`:

```bash
docker compose logs -f finhouse-api | grep -iE "embed|ollama|api"
```

You should see embeddings go to FPT Cloud URL and Ollama calls go to `<ip2>`.

---

## Switching back to PRIMARY (when ip1 recovers)

### Step 1 — On `<ip2>` (give up LLM role)

```bash
./scripts/switch-mode.sh model-primary
docker compose down
docker compose up -d
```

### Step 2 — On `<ip1>` (reclaim LLM)

```bash
./scripts/switch-mode.sh primary
docker compose down
docker compose up -d

# Pull models back on ip1 if not there
docker exec finhouse-ollama ollama pull qwen2.5:14b
```

---

## Quick reference

### Check current mode

```bash
./scripts/switch-mode.sh status
```

### File layout

```
FinHouse/
├── .env                    ← generated by switch-mode.sh (DO NOT edit directly)
├── .env.local              ← YOUR secrets + IPs (edit this once per host)
├── .env.backup.*           ← automatic backups before each switch
├── presets/
│   ├── .env.primary        ← "ip1=LLM, ip2=embed+rerank" template
│   ├── .env.failover       ← "ip2=LLM, API=embed+rerank" template
│   └── .env.model-host     ← template for ip2
└── scripts/
    └── switch-mode.sh
```

### What happens when you switch

1. `switch-mode.sh MODE` reads `.env.local` (your secrets + IPs)
2. Copies `presets/.env.MODE` to a staging file
3. Substitutes `<ip1>` and `<ip2>` placeholders
4. Overlays your secrets from `.env.local` on top
5. Backs up old `.env` to `.env.backup.TIMESTAMP`
6. Installs new `.env`

Your secrets stay in `.env.local` — never lost when switching.

### Commands cheatsheet

| Situation | Host | Command |
|---|---|---|
| Normal ops | ip1 | `switch-mode.sh primary && docker compose up -d` |
| Normal ops | ip2 | `switch-mode.sh model-primary && docker compose up -d` |
| ip1 GPU dies | ip2 | `switch-mode.sh model-failover && docker compose down && docker compose up -d` |
| ip1 GPU dies | ip1 | `switch-mode.sh failover && docker compose down && docker compose up -d` |
| ip1 recovered | ip1 | `switch-mode.sh primary && docker compose down && docker compose up -d` |
| ip1 recovered | ip2 | `switch-mode.sh model-primary && docker compose down && docker compose up -d` |
| Check status | either | `switch-mode.sh status` |

---

## Why this design

**Not pure automatic failover.** Auto-failover between hosts is attractive
but adds complexity — health check loops, race conditions, split-brain
scenarios. For a self-hosted RAG system with one operator, a two-command
manual switch is safer and more predictable.

**`.env.local` as the single source of truth.** Secrets live in one file.
Mode switches only change service topology (which containers run where,
which URLs the app calls). Your passwords and API keys never change when
you flip modes.

**Preset files are IP-agnostic.** They use `<ip1>` and `<ip2>` placeholders,
substituted at switch time. You can `git commit` the presets without
leaking IPs.

**Failover mode uses managed API, not local GPU on ip2.** Because the
LLM alone takes ~10 GB VRAM on ip2, there's no room left for embed+rerank
on a single consumer GPU. Managed API bridges this gap for <$5 for the
initial ingest + a few cents per chat session.

---

## Troubleshooting

### "IP2 is not set" warning

Edit `.env.local`, set `IP2=<actual_ip>`, re-run `switch-mode.sh`.

### Mode switch didn't change URLs

Check `.env.local` was read. Run `./scripts/switch-mode.sh status` — if
it shows old URLs, the preset substitution failed. Look at `.env` directly:

```bash
cat .env | grep HOST=
```

If placeholders `<ip1>`, `<ip2>` are still there, the sed substitution
didn't match. Make sure `.env.local` has `IP1=` and `IP2=` with no spaces
around `=`.

### After failover, chat is slow

Managed API adds ~100-300ms per embedding vs local GPU. Chat responses
should still feel acceptable for interactive use. If you're doing bulk
ingest (thousands of files), consider re-ingesting after primary is back.

### Want automatic failover instead

Requires one of:
- External health checker (e.g. systemd timer on ip1) that edits `.env`
  and calls `docker compose restart finhouse-api` when ip1 GPU is down
- Kubernetes with readiness probes
- HAProxy in front of both Ollama endpoints

All doable on top of this manual setup, but out of scope here.