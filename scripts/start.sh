#!/bin/bash
# ════════════════════════════════════════════════════════════
# FinHouse — Stack Runner
# ════════════════════════════════════════════════════════════
# Run any combination of stacks on this host.
#
# Usage:
#   ./start.sh <stack> [action]
#
# Stacks:
#   system    — API, UI, Postgres, MinIO, Milvus, etcd, SearXNG
#   ollama    — Ollama LLM + model puller
#   models    — BGE-M3 embed + reranker
#   all       — all three above
#
# Actions (default: up):
#   up        — start (detached)
#   down      — stop + remove containers
#   restart   — down + up
#   logs      — follow logs
#   ps        — list running containers
#   build     — rebuild images (no start)
#   pull      — pull latest images
#
# Examples:
#   ./start.sh system               # start the app stack
#   ./start.sh models               # start embed+rerank
#   ./start.sh ollama down          # stop Ollama only
#   ./start.sh all logs             # tail logs from all stacks
# ════════════════════════════════════════════════════════════

set -e

cd "$(dirname "$0")"
PROJECT_ROOT="$(pwd)"

STACK="${1:-}"
ACTION="${2:-up}"

STACK_FILES=()
case "$STACK" in
    system)    STACK_FILES=("stacks/system.yml") ;;
    ollama)    STACK_FILES=("stacks/ollama.yml") ;;
    models)    STACK_FILES=("stacks/models.yml") ;;
    pipeline)  STACK_FILES=("stacks/pipeline.yml") ;;
    all)       STACK_FILES=(
                   "stacks/system.yml"
                   "stacks/ollama.yml"
                   "stacks/models.yml"
                   "stacks/pipeline.yml"
               ) ;;
    -h|--help|help|"")
        sed -n '3,30p' "$0"
        exit 0
        ;;
    *)
        echo "❌ Unknown stack: $STACK"
        echo "Valid: system | ollama | models | pipeline | all"
        exit 1
        ;;
esac

# Build -f args for docker compose
COMPOSE_ARGS=()
for f in "${STACK_FILES[@]}"; do
    if [ ! -f "$f" ]; then
        echo "❌ Compose file missing: $f"
        exit 1
    fi
    COMPOSE_ARGS+=(-f "$f")
done

# Ensure .env exists (needed for variable interpolation)
if [ ! -f .env ]; then
    echo "⚠️  No .env file found."
    if [ -f .env.example ]; then
        echo "   Copy .env.example to .env and edit it first:"
        echo "     cp .env.example .env && nano .env"
    fi
    exit 1
fi

# ── Execute action ──────────────────────────────────────────
case "$ACTION" in
    up)
        echo "→ Starting $STACK stack..."
        docker compose "${COMPOSE_ARGS[@]}" up -d
        echo ""
        docker compose "${COMPOSE_ARGS[@]}" ps
        ;;
    down)
        echo "→ Stopping $STACK stack..."
        docker compose "${COMPOSE_ARGS[@]}" down
        ;;
    restart)
        echo "→ Restarting $STACK stack..."
        docker compose "${COMPOSE_ARGS[@]}" down
        docker compose "${COMPOSE_ARGS[@]}" up -d
        ;;
    logs)
        docker compose "${COMPOSE_ARGS[@]}" logs -f
        ;;
    ps)
        docker compose "${COMPOSE_ARGS[@]}" ps
        ;;
    build)
        echo "→ Building images for $STACK..."
        docker compose "${COMPOSE_ARGS[@]}" build
        ;;
    pull)
        docker compose "${COMPOSE_ARGS[@]}" pull
        ;;
    *)
        echo "❌ Unknown action: $ACTION"
        echo "Valid: up | down | restart | logs | ps | build | pull"
        exit 1
        ;;
esac
