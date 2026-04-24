"""
FinHouse — Data Folder Scanner
On startup, scans ./data directory for files and ingests any that haven't
been successfully processed before. Unsupported formats are marked as 'failed'.
"""

import os
import hashlib
import logging
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from models import File, Project
from services.ingest import (
    SUPPORTED_EXTENSIONS,
    ingest_file,
    upload_to_minio,
)

settings = get_settings()
logger = logging.getLogger("finhouse.scanner")

# The data directory (mounted into the container)
DATA_DIR = os.getenv("DATA_DIR", "/app/data")

# All files from ./data are assigned to:
#   user_id = 0 (system)
#   project_id = 0 (default inbox)
SYSTEM_USER_ID = 0
SYSTEM_PROJECT_ID = 0


def _compute_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _get_extension(filename: str) -> str:
    if "." in filename:
        return filename.rsplit(".", 1)[-1].lower()
    return ""


async def scan_data_folder(db: AsyncSession):
    """
    Scan the DATA_DIR folder and process files:
    1. For each file in ./data (recursively):
       a. Compute SHA-256 hash
       b. Check if already in DB with status 'ready' → skip
       c. Check if in DB with status 'failed' or 'pending' or 'processing' → retry
       d. If not in DB → new file, insert + process
    2. Unsupported extensions → insert with status 'failed'
    """
    data_path = Path(DATA_DIR)
    if not data_path.exists():
        logger.info(f"Data directory {DATA_DIR} does not exist, creating it...")
        data_path.mkdir(parents=True, exist_ok=True)
        return

    # Folders to skip entirely. These contain data for other pipelines
    # (OLAP ingestion to ClickHouse) and shouldn't be embedded as RAG chunks.
    SKIP_DIRS = {"OLAP", "checkpoint", "_ingestion_log", "logs", "tmp"}

    # Collect all files (recursive)
    all_files = []
    for root, dirs, files in os.walk(data_path):
        # Skip hidden directories + well-known non-RAG dirs
        dirs[:] = [
            d for d in dirs
            if not d.startswith(".") and d not in SKIP_DIRS
        ]
        for fname in files:
            if fname.startswith("."):
                continue
            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, data_path)
            all_files.append((full_path, rel_path, fname))

    if not all_files:
        logger.info(f"No files found in {DATA_DIR} (excluding {SKIP_DIRS})")
        return

    logger.info(
        f"📂 Found {len(all_files)} files in {DATA_DIR} "
        f"(skipped dirs: {SKIP_DIRS})"
    )

    processed = 0
    skipped = 0
    failed = 0
    retried = 0

    for full_path, rel_path, fname in all_files:
        ext = _get_extension(fname)

        try:
            content = open(full_path, "rb").read()
        except Exception as e:
            logger.error(f"Cannot read {rel_path}: {e}")
            failed += 1
            continue

        file_hash = _compute_hash(content)

        # Check if file already exists in DB (by hash + default project)
        result = await db.execute(
            select(File).where(
                File.file_hash == file_hash,
                File.project_id == SYSTEM_PROJECT_ID,
            )
        )
        existing = result.scalar_one_or_none()

        if existing:
            if existing.process_status == "ready":
                logger.debug(f"✓ Already processed: {rel_path}")
                skipped += 1
                continue
            elif existing.process_status in ("failed", "pending", "processing"):
                # Retry: re-process this file
                logger.info(f"🔄 Retrying previously {existing.process_status} file: {rel_path}")
                existing.process_status = "processing"
                await db.flush()
                file_record = existing
                retried += 1
            else:
                skipped += 1
                continue
        else:
            # New file — create DB record
            # Determine MinIO path
            object_name = f"data/{rel_path}"

            # Upload to MinIO
            try:
                upload_to_minio(content, object_name)
            except Exception as e:
                logger.error(f"MinIO upload failed for {rel_path}: {e}")
                failed += 1
                continue

            file_record = File(
                user_id=SYSTEM_USER_ID,
                project_id=SYSTEM_PROJECT_ID,
                session_id=None,
                file_hash=file_hash,
                file_name=fname,
                file_type=ext if ext else "unknown",
                process_status="processing",
                file_dir=f"{settings.MINIO_BUCKET}/{object_name}",
            )
            db.add(file_record)
            await db.flush()

        # ── Process the file ────────────────────────────────
        file_id_str = str(file_record.file_id)

        if ext not in SUPPORTED_EXTENSIONS:
            logger.warning(f"✗ Unsupported format '{ext}': {rel_path} → marking as failed")
            file_record.process_status = "failed"
            file_record.process_at = datetime.now(timezone.utc)
            await db.flush()
            failed += 1
            continue

        # Run ingest
        try:
            ingest_result = await ingest_file(
                file_id=file_id_str,
                file_content=content,
                file_name=fname,
                file_type=ext,
                project_id=SYSTEM_PROJECT_ID,
            )

            file_record.process_status = ingest_result["status"]
            file_record.process_at = datetime.now(timezone.utc)
            await db.flush()

            if ingest_result["status"] == "ready":
                logger.info(
                    f"✅ Processed: {rel_path} → {ingest_result['chunks_count']} chunks"
                )
                processed += 1
            else:
                logger.warning(
                    f"✗ Failed: {rel_path} → {ingest_result.get('error', 'unknown')}"
                )
                failed += 1

        except Exception as e:
            logger.error(f"Ingest exception for {rel_path}: {e}", exc_info=True)
            file_record.process_status = "failed"
            file_record.process_at = datetime.now(timezone.utc)
            await db.flush()
            failed += 1

    await db.commit()

    logger.info(
        f"📊 Data scan complete: "
        f"{processed} processed, {retried} retried, {skipped} skipped, {failed} failed "
        f"(total: {len(all_files)} files)"
    )


async def run_startup_scan():
    """
    Entry point called from FastAPI lifespan.
    Creates its own DB session for the scan.

    This runs in the background — the API accepts requests immediately
    instead of blocking until scan completes. Users can chat while data
    is being indexed; RAG simply returns nothing for queries before
    indexing finishes.
    """
    from database import async_session_factory

    # Wait for Milvus to be ready before scanning (Milvus takes 2-5 min to boot)
    logger.info("⏳ Waiting for Milvus to be ready...")
    if not await _wait_for_milvus(max_wait_seconds=600):
        logger.warning(
            "⚠️  Milvus not ready after 10 minutes — skipping data folder scan. "
            "Files in ./data will not be auto-ingested. Restart the API container "
            "once Milvus is stable, or upload files via the UI."
        )
        return

    logger.info(f"🔍 Starting data folder scan: {DATA_DIR}")

    async with async_session_factory() as db:
        try:
            await scan_data_folder(db)
        except Exception as e:
            logger.error(f"Data scan failed: {e}", exc_info=True)


async def kick_off_background_scan():
    """
    Launch the scan as a background task without awaiting it.
    Called from FastAPI lifespan to avoid blocking API startup.
    """
    import asyncio
    asyncio.create_task(run_startup_scan())


async def _wait_for_milvus(max_wait_seconds: int = 600) -> bool:
    """
    Poll Milvus healthz endpoint until ready, or timeout.
    Returns True if ready, False if timed out.
    Uses a single httpx client across the whole polling loop.
    """
    import asyncio
    import httpx

    milvus_health_url = f"http://{settings.MILVUS_HOST}:9091/healthz"
    check_interval = 5  # seconds
    elapsed = 0

    async with httpx.AsyncClient(timeout=5.0) as client:
        while elapsed < max_wait_seconds:
            try:
                resp = await client.get(milvus_health_url)
                if resp.status_code == 200:
                    logger.info(f"✅ Milvus ready after {elapsed}s")
                    return True
            except Exception:
                pass  # still booting

            if elapsed % 30 == 0 and elapsed > 0:
                logger.info(f"   still waiting for Milvus... ({elapsed}s elapsed)")

            await asyncio.sleep(check_interval)
            elapsed += check_interval

    return False