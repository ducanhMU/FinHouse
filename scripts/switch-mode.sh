#!/bin/bash
# ════════════════════════════════════════════════════════════
# FinHouse — Mode Switch Script
# ════════════════════════════════════════════════════════════
# One-command switch between deployment modes.
#
# Usage:
#   ./scripts/switch-mode.sh primary          # ip1=LLM, ip2=embed+rerank
#   ./scripts/switch-mode.sh failover         # ip2=LLM, API=embed+rerank
#   ./scripts/switch-mode.sh model-primary    # (on ip2) embed+rerank
#   ./scripts/switch-mode.sh model-failover   # (on ip2) LLM only
#   ./scripts/switch-mode.sh status
#
# First run: edit .env.local with your IPs + API keys, then run
# `./scripts/switch-mode.sh primary` to generate .env.
# ════════════════════════════════════════════════════════════

set -e

cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"
PRESETS_DIR="$PROJECT_ROOT/presets"
LOCAL_ENV="$PROJECT_ROOT/.env.local"

MODE="${1:-status}"

# ── Ensure .env.local exists (stores user-specific values) ──
ensure_local_env() {
    if [ ! -f "$LOCAL_ENV" ]; then
        cat > "$LOCAL_ENV" <<'EOF'
# ════════════════════════════════════════════════════════════
# FinHouse — Local User Overrides
# ════════════════════════════════════════════════════════════
# Fill in YOUR values here. These are preserved across mode
# switches — .env is regenerated from presets + these values.
#
# Edit this file ONCE on each host. Then use switch-mode.sh
# to flip between modes without touching .env directly.
# ════════════════════════════════════════════════════════════

# ── IP addresses of your servers ────────────────────────────
IP1=127.0.0.1
IP2=127.0.0.1

# ── Secrets ─────────────────────────────────────────────────
POSTGRES_PASSWORD=changeme_pg_secret
MINIO_ROOT_PASSWORD=changeme_minio_secret
JWT_SECRET=changeme_jwt_secret_at_least_32_chars

# ── Managed API credentials (required for failover) ─────────
EMBED_API_URL=https://mkp-api.fptcloud.com/v1
EMBED_API_KEY=
EMBED_API_MODEL=Vietnamese_Embedding
EMBED_API_DIMENSIONS=1024

RERANK_API_URL=https://mkp-api.fptcloud.com/v1
RERANK_API_KEY=
RERANK_API_MODEL=bge-reranker-v2-m3

# ── Other overrides ─────────────────────────────────────────
CORS_ALLOW_ORIGINS=http://localhost:8501,http://127.0.0.1:8501
ENV=dev
EOF
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "✓ Created $LOCAL_ENV"
        echo ""
        echo "EDIT IT NOW before running switch-mode again:"
        echo "  - Set IP1 and IP2 to your server addresses"
        echo "  - Set real passwords (if going to production)"
        echo "  - Set EMBED_API_KEY and RERANK_API_KEY if using failover"
        echo ""
        echo "Then re-run: $0 $MODE"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        exit 0
    fi
}

# ── Load .env.local into shell variables ────────────────────
load_local_env() {
    set -a
    # shellcheck disable=SC1090
    source "$LOCAL_ENV"
    set +a
}

# ── Substitute <ip1>, <ip2>, and preserve-keys into preset ──
apply_preset() {
    local preset_file="$1"
    local target=".env.new"

    if [ ! -f "$preset_file" ]; then
        echo "❌ Preset not found: $preset_file"
        exit 1
    fi

    # Step 1: substitute IP placeholders
    sed -e "s|<ip1>|${IP1}|g" \
        -e "s|<ip2>|${IP2}|g" \
        "$preset_file" > "$target"

    # Step 2: overwrite preserved keys with values from .env.local
    local keys=(
        POSTGRES_PASSWORD MINIO_ROOT_PASSWORD JWT_SECRET
        EMBED_API_URL EMBED_API_KEY EMBED_API_MODEL EMBED_API_DIMENSIONS
        RERANK_API_URL RERANK_API_KEY RERANK_API_MODEL
        CORS_ALLOW_ORIGINS ENV
    )
    for key in "${keys[@]}"; do
        local value="${!key}"
        if [ -n "$value" ]; then
            # Replace existing key in target
            grep -v "^${key}=" "$target" > "$target.tmp" && mv "$target.tmp" "$target"
            echo "${key}=${value}" >> "$target"
        fi
    done

    # Step 3: backup current, install new
    if [ -f .env ]; then
        local ts=$(date +%Y%m%d-%H%M%S)
        cp .env ".env.backup.${ts}"
    fi
    mv "$target" .env
    echo "✓ Switched to: $(basename "$preset_file")"
}

# ── Show current configuration ──────────────────────────────
show_status() {
    if [ ! -f .env ]; then
        echo "❌ No .env file. Run with a mode to generate one."
        return 1
    fi

    echo "Current .env summary:"
    echo "────────────────────────────────────────────────────"
    grep -E "^(COMPOSE_PROFILES|OLLAMA_HOST|EMBED_HOST|RERANK_HOST|EMBED_API_URL|RERANK_API_URL)=" .env \
        | sed -E 's/(API_KEY|PASSWORD|SECRET)=.+/\1=***/'
    echo ""

    local profiles=$(grep "^COMPOSE_PROFILES=" .env | cut -d= -f2-)
    local embed=$(grep "^EMBED_HOST=" .env | cut -d= -f2-)
    local ollama=$(grep "^OLLAMA_HOST=" .env | cut -d= -f2-)

    echo "Detected mode:"
    if [[ "$profiles" == *"local-llm"* ]] && [[ -n "$embed" ]] && [[ "$embed" != *"finhouse-"* ]]; then
        echo "  📡 PRIMARY (this host=LLM, remote=embed+rerank)"
    elif [ -z "$profiles" ] && [ -z "$embed" ]; then
        echo "  🆘 FAILOVER (ip2=LLM, API=embed+rerank)"
    elif [[ "$profiles" == *"local-llm"* ]] && [[ "$profiles" == *"local-embed"* ]]; then
        echo "  🏠 ALL-LOCAL (everything on this host)"
    elif [[ "$profiles" == "local-embed,local-rerank" ]]; then
        echo "  🤖 MODEL-PRIMARY (this host = embed+rerank)"
    elif [[ "$profiles" == "local-llm" ]] && [[ "$ollama" == *"finhouse-ollama"* ]]; then
        echo "  🤖 MODEL-FAILOVER (this host = LLM only)"
    else
        echo "  ❓ Custom / mixed configuration"
    fi
}

# ── Validate prerequisites ──────────────────────────────────
validate_for_failover() {
    if [ -z "$EMBED_API_KEY" ] || [ -z "$RERANK_API_KEY" ]; then
        echo "⚠️  WARNING: EMBED_API_KEY or RERANK_API_KEY is empty in .env.local"
        echo "   Failover mode needs these to embed/rerank without local GPU."
        echo "   Edit .env.local first, then re-run."
        return 1
    fi
    if [ -z "$IP2" ] || [ "$IP2" = "127.0.0.1" ]; then
        echo "⚠️  WARNING: IP2 is not set in .env.local"
        echo "   Failover needs ip2 to host Ollama."
        return 1
    fi
    return 0
}

validate_for_primary() {
    if [ -z "$IP2" ] || [ "$IP2" = "127.0.0.1" ]; then
        echo "⚠️  WARNING: IP2 is not set in .env.local"
        echo "   Primary mode needs ip2 for embed+rerank services."
        return 1
    fi
    return 0
}

# ════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════

case "$MODE" in
    status)
        show_status
        ;;

    primary)
        ensure_local_env
        load_local_env
        validate_for_primary || true
        echo "Switching to PRIMARY mode..."
        apply_preset "$PRESETS_DIR/.env.primary"
        echo ""
        echo "Next steps:"
        echo "  1. On ip2 ($IP2): ./scripts/switch-mode.sh model-primary"
        echo "                    docker compose up -d"
        echo "  2. On this host:  docker compose up -d"
        ;;

    failover)
        ensure_local_env
        load_local_env
        validate_for_failover || echo "(proceeding anyway — fix before deploy)"
        echo "Switching to FAILOVER mode..."
        apply_preset "$PRESETS_DIR/.env.failover"
        echo ""
        echo "Next steps:"
        echo "  1. On ip2 ($IP2): ./scripts/switch-mode.sh model-failover"
        echo "                    docker exec finhouse-ollama ollama pull qwen2.5:14b"
        echo "                    docker compose up -d"
        echo "  2. On this host:  docker compose restart finhouse-api"
        ;;

    model-primary)
        ensure_local_env
        load_local_env
        echo "Switching MODEL HOST to PRIMARY (embed+rerank)..."
        apply_preset "$PRESETS_DIR/.env.model-host"
        # Force primary profile
        sed -i.bak 's|^COMPOSE_PROFILES=.*|COMPOSE_PROFILES=local-embed,local-rerank|' .env
        rm -f .env.bak
        echo "✓ This host now runs embed+rerank only"
        echo "  Apply: docker compose down && docker compose up -d"
        ;;

    model-failover)
        ensure_local_env
        load_local_env
        echo "Switching MODEL HOST to FAILOVER (Ollama LLM only)..."
        apply_preset "$PRESETS_DIR/.env.model-host"
        sed -i.bak 's|^COMPOSE_PROFILES=.*|COMPOSE_PROFILES=local-llm|' .env
        rm -f .env.bak
        echo "✓ This host now runs Ollama only"
        echo "  Apply: docker compose down && docker compose up -d"
        echo ""
        echo "  Then pull LLMs:"
        echo "    docker exec finhouse-ollama ollama pull qwen2.5:14b"
        echo "    docker exec finhouse-ollama ollama pull llama3.1:8b"
        ;;

    -h|--help|help)
        sed -n '3,20p' "$0"
        ;;

    *)
        echo "❌ Unknown mode: $MODE"
        echo "Valid: primary | failover | model-primary | model-failover | status"
        exit 1
        ;;
esac
