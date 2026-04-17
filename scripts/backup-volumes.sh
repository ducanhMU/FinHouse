#!/bin/bash
# ============================================================
# FinHouse — Volume Backup Script
# ============================================================
# Creates a consistent snapshot of all persistent state by
# stopping containers, tarring volumes, and restarting.
#
# Output: finhouse-backup-YYYYMMDD-HHMMSS.tar.gz in current dir
# Usage:
#   ./scripts/backup-volumes.sh                  # backup all
#   ./scripts/backup-volumes.sh --no-models      # skip heavy Ollama/BGE models
#   ./scripts/backup-volumes.sh --no-stop        # online backup (risky but fast)
# ============================================================

set -e

# ── Parse args ──────────────────────────────────────────────
INCLUDE_MODELS=true
STOP_SERVICES=true

for arg in "$@"; do
    case "$arg" in
        --no-models) INCLUDE_MODELS=false ;;
        --no-stop)   STOP_SERVICES=false ;;
        -h|--help)
            sed -n '3,15p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown arg: $arg"
            exit 1
            ;;
    esac
done

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="finhouse-backup-${TIMESTAMP}"
ARCHIVE_NAME="${BACKUP_DIR}.tar.gz"

cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"

if [ ! -d "./volumes" ]; then
    echo "ERROR: ./volumes directory not found. Are you in the project root?"
    exit 1
fi

echo "=================================================="
echo "FinHouse volume backup"
echo "=================================================="
echo "Timestamp:       $TIMESTAMP"
echo "Include models:  $INCLUDE_MODELS"
echo "Stop services:   $STOP_SERVICES"
echo "Project root:    $PROJECT_ROOT"
echo "=================================================="

# ── Stop services for consistency ───────────────────────────
if [ "$STOP_SERVICES" = true ]; then
    echo ""
    echo "→ Stopping containers for consistent snapshot..."
    docker compose stop 2>/dev/null || true
else
    echo ""
    echo "⚠️  Online backup mode — data may be inconsistent if writes happen."
fi

# ── Build exclude list ──────────────────────────────────────
EXCLUDES=()
if [ "$INCLUDE_MODELS" = false ]; then
    EXCLUDES+=(--exclude='volumes/ollama')
fi

# Always exclude scratch data — not worth backing up
EXCLUDES+=(--exclude='volumes/*/tmp')
EXCLUDES+=(--exclude='volumes/*/.cache')

# ── Create archive ──────────────────────────────────────────
echo ""
echo "→ Creating archive: $ARCHIVE_NAME"
echo "  (this can take several minutes for large datasets)"

# Capture versions into a manifest so we can detect incompatibility on restore
MANIFEST_FILE="volumes/.backup-manifest.txt"
{
    echo "FinHouse backup manifest"
    echo "Created: $(date -Iseconds)"
    echo "Host: $(hostname)"
    echo ""
    echo "## Docker images used at backup time:"
    docker compose images 2>/dev/null || docker compose config --images 2>/dev/null || true
    echo ""
    echo "## Include models: $INCLUDE_MODELS"
    echo "## Stop services: $STOP_SERVICES"
} > "$MANIFEST_FILE"

tar --warning=no-file-changed \
    "${EXCLUDES[@]}" \
    -czf "$ARCHIVE_NAME" \
    volumes/ \
    .env \
    docker-compose.yml \
    2>&1 | grep -v "Removing leading" || true

rm -f "$MANIFEST_FILE"

# ── Restart services ────────────────────────────────────────
if [ "$STOP_SERVICES" = true ]; then
    echo ""
    echo "→ Restarting containers..."
    docker compose up -d 2>/dev/null || true
fi

# ── Summary ─────────────────────────────────────────────────
SIZE=$(du -h "$ARCHIVE_NAME" | cut -f1)
echo ""
echo "=================================================="
echo "✅ Backup complete"
echo "   File: $ARCHIVE_NAME"
echo "   Size: $SIZE"
echo ""
echo "Transfer to another host:"
echo "   scp $ARCHIVE_NAME user@remote:/path/to/finhouse/"
echo ""
echo "Restore on target host:"
echo "   cd /path/to/finhouse/"
echo "   ./scripts/restore-volumes.sh $ARCHIVE_NAME"
echo "=================================================="
