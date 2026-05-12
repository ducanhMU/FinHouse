"""
RAG v1 → v2 migration.

Re-ingests every file that lives in the legacy `finhouse_chunks` collection
into the v2 `finhouse_chunks_v2` collection (HNSW dense + sparse hybrid).
Source documents are pulled from MinIO so the original text is rechunked
(can use semantic chunking if RAG_SEMANTIC_CHUNKING=true) and re-embedded
with both dense AND sparse vectors via the upgraded embed service.

The legacy collection is **NOT touched**. Rollback = set
RAG_COLLECTION=finhouse_chunks in .env and restart the API.

Usage (inside the API container, or anywhere with the same env / network):
    python scripts/migrate_rag_v2.py                # migrate all ready files
    python scripts/migrate_rag_v2.py --project 0    # only base knowledge
    python scripts/migrate_rag_v2.py --dry-run      # list files only

The v2 collection is created on first insert via ingest._get_milvus_connection().
"""

import argparse
import asyncio
import logging
import os
import sys

# Make `api/` importable so we can reuse config + ingest helpers
HERE = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(os.path.dirname(HERE), "api")
sys.path.insert(0, API_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("finhouse.migrate")


async def _list_ready_files(project_filter: int | None) -> list[dict]:
    """Pull file rows from Postgres so we know what to re-ingest."""
    from sqlalchemy import select
    from database import async_session_factory
    from models import File

    async with async_session_factory() as db:
        stmt = select(File).where(File.process_status == "ready")
        if project_filter is not None:
            stmt = stmt.where(File.project_id == project_filter)
        result = await db.execute(stmt)
        rows = result.scalars().all()
        return [
            {
                "file_id": str(r.file_id),
                "file_name": r.file_name,
                "file_type": r.file_type,
                "project_id": r.project_id,
                "file_dir": r.file_dir,
            }
            for r in rows
        ]


async def migrate_one(meta: dict) -> tuple[bool, str]:
    """Download from MinIO and re-run ingest_file → writes to active collection."""
    from services.ingest import download_from_minio, ingest_file

    try:
        content = download_from_minio(meta["file_dir"])
    except Exception as e:
        return False, f"download failed: {e}"

    res = await ingest_file(
        file_id=meta["file_id"],
        file_content=content,
        file_name=meta["file_name"],
        file_type=meta["file_type"],
        project_id=meta["project_id"],
    )
    if res.get("status") != "ready":
        return False, f"ingest failed: {res.get('error')}"
    return True, f"{res.get('chunks_count', 0)} chunks"


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project", type=int, default=None,
        help="Only migrate files in this project_id (e.g. 0 for base knowledge)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List files that would be migrated and exit",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Stop after N files (useful for smoke-testing)",
    )
    args = parser.parse_args()

    from config import get_settings
    settings = get_settings()

    if settings.RAG_COLLECTION == "finhouse_chunks":
        log.error(
            "RAG_COLLECTION=finhouse_chunks (legacy). Set "
            "RAG_COLLECTION=finhouse_chunks_v2 in .env before running migration."
        )
        sys.exit(1)

    log.info(
        "Migrating into collection=%s (hybrid=%s, semantic_chunking=%s)",
        settings.RAG_COLLECTION,
        settings.RAG_HYBRID_ENABLED,
        settings.RAG_SEMANTIC_CHUNKING,
    )

    files = await _list_ready_files(args.project)
    if args.limit:
        files = files[: args.limit]

    if not files:
        log.warning("No files found matching filter — nothing to migrate.")
        return

    log.info("Found %d files to migrate.", len(files))
    if args.dry_run:
        for f in files:
            log.info("  - [%s] %s (project=%s)", f["file_id"], f["file_name"], f["project_id"])
        return

    ok = 0
    failed: list[tuple[str, str]] = []
    for i, meta in enumerate(files, 1):
        log.info(
            "[%d/%d] %s (project=%s)...",
            i, len(files), meta["file_name"], meta["project_id"],
        )
        try:
            success, info = await migrate_one(meta)
        except Exception as e:
            success, info = False, f"crash: {e}"
        if success:
            ok += 1
            log.info("  ✓ %s", info)
        else:
            failed.append((meta["file_name"], info))
            log.error("  ✗ %s", info)

    log.info("Migration done: %d ok / %d failed", ok, len(failed))
    if failed:
        log.warning("Failures:")
        for name, why in failed:
            log.warning("  %s — %s", name, why)


if __name__ == "__main__":
    asyncio.run(main())
