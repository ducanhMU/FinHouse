"""
FinHouse — Cleanup Scheduler

Runs periodically (every CLEANUP_INTERVAL_MINUTES). Responsibilities:

1. Purge files marked `process_status='deleted'` older than grace period:
     • Remove Milvus vector chunks
     • Remove MinIO object
     • Delete the Postgres row

2. Purge orphaned incognito projects with negative project_id and no
   associated sessions older than `INCOGNITO_TTL_HOURS`.

3. Trim `ChatEvent` rows in "message" type older than `CHAT_EVENT_TTL_DAYS`
   for incognito sessions (privacy hygiene).

Design:
   • Grace period gives ops a chance to "un-delete" by flipping the flag
     back before physical removal happens.
   • One run = one database session. Errors in one file don't abort
     others — we log and continue.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from database import async_session_factory
from models import File, Project, ChatSession, ChatEvent

settings = get_settings()
logger = logging.getLogger("finhouse.cleanup")

# Defaults (override via env vars if needed — wire them in config.py)
DELETED_GRACE_MINUTES = 60        # keep "deleted" files for 1h before purge
INCOGNITO_TTL_HOURS = 24          # incognito projects expire after 24h
CHAT_EVENT_TTL_DAYS = 7           # events in incognito projects purged after 7d


async def _purge_deleted_files(db: AsyncSession) -> int:
    """
    Delete Postgres rows for files flagged 'deleted' older than grace period.
    The actual MinIO + Milvus purge happens here too (belt-and-suspenders:
    the DELETE endpoint also calls delete_file_chunks, but this catches
    anything that slipped through or was manually flagged).
    """
    from services.ingest import delete_file_chunks, delete_file_object

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=DELETED_GRACE_MINUTES)

    result = await db.execute(
        select(File).where(
            File.process_status == "deleted",
            File.process_at <= cutoff,
        )
    )
    files_to_purge = list(result.scalars().all())

    if not files_to_purge:
        return 0

    logger.info(f"Purging {len(files_to_purge)} file(s) older than {cutoff.isoformat()}")

    purged = 0
    for f in files_to_purge:
        try:
            # Milvus chunks (idempotent)
            try:
                delete_file_chunks(str(f.file_id))
            except Exception as e:
                logger.warning(f"[{f.file_id}] Milvus purge failed: {e}")

            # MinIO object (if still present)
            try:
                if f.file_dir and "/" in f.file_dir:
                    bucket, _, object_name = f.file_dir.partition("/")
                    delete_file_object(bucket, object_name)
            except Exception as e:
                logger.warning(f"[{f.file_id}] MinIO purge failed: {e}")

            # Postgres row (hard delete)
            await db.delete(f)
            purged += 1
        except Exception as e:
            logger.error(f"[{f.file_id}] purge error: {e}")

    await db.commit()
    logger.info(f"✓ Purged {purged} file rows")
    return purged


async def _purge_expired_incognito(db: AsyncSession) -> int:
    """
    Remove incognito projects (negative project_id) with no recent activity.
    Cascades to sessions via FK ON DELETE, but we do it explicitly for safety.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=INCOGNITO_TTL_HOURS)

    # Find stale incognito projects — those whose newest session hasn't been
    # updated recently. We use a LEFT JOIN to also catch projects with no sessions.
    from sqlalchemy import func
    result = await db.execute(
        select(Project.project_id)
        .outerjoin(ChatSession, ChatSession.project_id == Project.project_id)
        .where(Project.project_id < 0)
        .group_by(Project.project_id)
        .having(
            (func.max(ChatSession.update_at).is_(None)) |
            (func.max(ChatSession.update_at) < cutoff)
        )
    )
    stale_ids = [row[0] for row in result.all()]

    if not stale_ids:
        return 0

    logger.info(f"Deleting {len(stale_ids)} stale incognito project(s)")

    # Delete sessions first
    await db.execute(delete(ChatSession).where(ChatSession.project_id.in_(stale_ids)))
    # Then the projects themselves
    await db.execute(delete(Project).where(Project.project_id.in_(stale_ids)))
    await db.commit()

    return len(stale_ids)


async def _trim_old_chat_events(db: AsyncSession) -> int:
    """
    Remove message events older than TTL from incognito sessions only.
    Doesn't touch regular user data — that's theirs to manage.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=CHAT_EVENT_TTL_DAYS)

    # Get incognito session IDs
    result = await db.execute(
        select(ChatSession.session_id)
        .join(Project, ChatSession.project_id == Project.project_id)
        .where(Project.project_id < 0)
    )
    incognito_session_ids = [row[0] for row in result.all()]
    if not incognito_session_ids:
        return 0

    # Delete old message events (keeps checkpoints/summaries slightly longer
    # would be a nice extension, but for now this is fine — the whole incognito
    # project gets GC'd by _purge_expired_incognito soon anyway).
    delete_stmt = delete(ChatEvent).where(
        ChatEvent.session_id.in_(incognito_session_ids),
        ChatEvent.create_at < cutoff,
    )
    result = await db.execute(delete_stmt)
    await db.commit()
    return result.rowcount or 0


# ────────────────────────────────────────────────────────────
# Entry point — called by lifespan
# ────────────────────────────────────────────────────────────

async def run_cleanup_cycle():
    """Single sweep. Safe to call concurrently — each uses its own session."""
    logger.info("🧹 Cleanup cycle starting")
    async with async_session_factory() as db:
        try:
            n_files = await _purge_deleted_files(db)
        except Exception as e:
            logger.error(f"purge_deleted_files failed: {e}", exc_info=True)
            n_files = 0

        try:
            n_projects = await _purge_expired_incognito(db)
        except Exception as e:
            logger.error(f"purge_expired_incognito failed: {e}", exc_info=True)
            n_projects = 0

        try:
            n_events = await _trim_old_chat_events(db)
        except Exception as e:
            logger.error(f"trim_old_chat_events failed: {e}", exc_info=True)
            n_events = 0

    logger.info(
        f"🧹 Cleanup done — "
        f"files purged: {n_files}, "
        f"incognito projects deleted: {n_projects}, "
        f"old events trimmed: {n_events}"
    )


async def cleanup_worker():
    """
    Background task: sleep CLEANUP_INTERVAL_MINUTES between cycles.
    Runs forever until cancelled.
    """
    interval = max(5, settings.CLEANUP_INTERVAL_MINUTES) * 60
    logger.info(f"Cleanup worker started (interval: {interval}s)")

    # Delay first run by 2 minutes — let the app warm up first
    await asyncio.sleep(120)

    while True:
        try:
            await run_cleanup_cycle()
        except asyncio.CancelledError:
            logger.info("Cleanup worker cancelled")
            raise
        except Exception as e:
            logger.error(f"Cleanup cycle threw unexpectedly: {e}", exc_info=True)
        await asyncio.sleep(interval)


def start_cleanup_task() -> asyncio.Task:
    """Fire-and-forget task starter. Returns the Task for cancellation on shutdown."""
    return asyncio.create_task(cleanup_worker())
