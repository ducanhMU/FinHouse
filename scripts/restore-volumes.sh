#!/bin/bash
# ============================================================
# FinHouse — Volume Restore Script
# ============================================================
# Extracts a volume backup into the project directory and
# restarts services. Safe-by-default: refuses to overwrite
# existing data unless --force is passed.
#
# Usage:
#   ./scripts/restore-volumes.sh backup.tar.gz           # safe mode
#   ./scripts/restore-volumes.sh backup.tar.gz --force   # overwrite existing
# ============================================================

set -e

if [ -z "$1" ]; then
    echo "Usage: $0 <backup.tar.gz> [--force]"
    exit 1
fi

ARCHIVE="$1"
FORCE=false
if [ "$2" = "--force" ]; then
    FORCE=true
fi

if [ ! -f "$ARCHIVE" ]; then
    echo "ERROR: Archive not found: $ARCHIVE"
    exit 1
fi

cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"

echo "=================================================="
echo "FinHouse volume restore"
echo "=================================================="
echo "Archive:      $ARCHIVE"
echo "Project root: $PROJECT_ROOT"
echo "Force mode:   $FORCE"
echo "=================================================="

# ── Safety check ────────────────────────────────────────────
if [ -d "./volumes" ] && [ "$FORCE" = false ]; then
    if [ "$(ls -A ./volumes 2>/dev/null)" ]; then
        echo ""
        echo "⚠️  ./volumes already contains data."
        echo "    Pass --force to overwrite (existing data will be DELETED)."
        exit 1
    fi
fi

# ── Inspect archive ─────────────────────────────────────────
echo ""
echo "→ Inspecting archive contents..."
FILE_COUNT=$(tar -tzf "$ARCHIVE" | wc -l)
echo "  $FILE_COUNT files inside"

if ! tar -tzf "$ARCHIVE" | grep -q '^volumes/'; then
    echo "ERROR: Archive does not contain volumes/ directory — wrong file?"
    exit 1
fi

# ── Stop services ──────────────────────────────────────────
echo ""
echo "→ Stopping existing containers..."
docker compose down 2>/dev/null || true

# ── Backup current state (just in case) ────────────────────
if [ -d "./volumes" ] && [ "$(ls -A ./volumes 2>/dev/null)" ]; then
    SAFETY_BACKUP="volumes.pre-restore.$(date +%s)"
    echo ""
    echo "→ Moving existing ./volumes to $SAFETY_BACKUP (safety backup)"
    mv volumes "$SAFETY_BACKUP"
fi

# ── Extract ─────────────────────────────────────────────────
echo ""
echo "→ Extracting $ARCHIVE ..."
tar -xzf "$ARCHIVE"

# Verify extraction
if [ ! -d "./volumes" ]; then
    echo "ERROR: Extraction did not produce ./volumes directory"
    exit 1
fi

# ── Permissions note ───────────────────────────────────────
echo ""
echo "→ Fixing permissions for Docker volumes..."
# Postgres runs as uid 999; MinIO/etcd/ollama use various uids.
# The containers will fix their own permissions on startup,
# but we need to ensure the host user can read/exec the directories.
chmod -R u+rwX volumes/ 2>/dev/null || true

# ── Start services ──────────────────────────────────────────
echo ""
echo "→ Starting containers..."
docker compose up -d

# ── Wait and verify ─────────────────────────────────────────
echo ""
echo "→ Waiting 15s for services to initialize..."
sleep 15

echo ""
echo "→ Service status:"
docker compose ps

echo ""
echo "=================================================="
echo "✅ Restore complete"
echo ""
echo "Verify data is readable:"
echo "   # PostgreSQL tables"
echo "   docker exec finhouse-postgres psql -U finhouse -d finhouse -c '\\dt'"
echo ""
echo "   # MinIO buckets"
echo "   docker exec finhouse-minio mc ls local/ || true"
echo ""
echo "   # Ollama models"
echo "   docker exec finhouse-ollama ollama list || true"
echo ""
echo "   # Check API health"
echo "   curl http://localhost:18000/health"
echo "=================================================="
