"""FinHouse — Files Router (upload, list, delete, status, re-process)."""

import hashlib
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import (
    APIRouter, Depends, HTTPException, UploadFile,
    File as FastAPIFile, Form, BackgroundTasks,
)
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, async_session_factory
from models import File
from routers.auth import get_current_user
from routers.sessions import _can_user_access_project
from config import get_settings
from services.ingest import (
    SUPPORTED_EXTENSIONS,
    ingest_file,
    upload_to_minio,
    delete_file_chunks,
    download_from_minio,
    MAX_FILE_SIZE_MB,
)

router = APIRouter(prefix="/files", tags=["files"])
settings = get_settings()

MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024


class FileOut(BaseModel):
    file_id: UUID
    user_id: int
    project_id: int
    session_id: Optional[UUID] = None
    file_name: str
    file_type: str
    process_status: str
    process_at: Optional[datetime] = None
    file_dir: str = ""

    class Config:
        from_attributes = True


async def _authorize_file(
    db: AsyncSession, user_id: int, file_id: UUID
) -> File:
    """Load file and verify the user can access its project."""
    result = await db.execute(select(File).where(File.file_id == file_id))
    f = result.scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="File not found")
    if not await _can_user_access_project(db, user_id, f.project_id):
        raise HTTPException(status_code=403, detail="Access denied")
    # Guests cannot see arbitrary project-0 files
    if user_id == 0 and f.project_id == 0:
        raise HTTPException(status_code=403, detail="Access denied")
    return f


# ── Background ingest task ──────────────────────────────────

async def _run_ingest_background(
    file_id: str, content: bytes, file_name: str, file_type: str, project_id: int
):
    async with async_session_factory() as db:
        try:
            result = await db.execute(
                select(File).where(File.file_id == UUID(file_id))
            )
            rec = result.scalar_one_or_none()
            if not rec:
                return

            rec.process_status = "processing"
            await db.commit()

            ingest_result = await ingest_file(
                file_id=file_id,
                file_content=content,
                file_name=file_name,
                file_type=file_type,
                project_id=project_id,
            )

            result = await db.execute(
                select(File).where(File.file_id == UUID(file_id))
            )
            rec = result.scalar_one_or_none()
            if rec:
                rec.process_status = ingest_result["status"]
                rec.process_at = datetime.now(timezone.utc)
                await db.commit()

        except Exception:
            try:
                result = await db.execute(
                    select(File).where(File.file_id == UUID(file_id))
                )
                rec = result.scalar_one_or_none()
                if rec:
                    rec.process_status = "failed"
                    rec.process_at = datetime.now(timezone.utc)
                    await db.commit()
            except Exception:
                pass


# ── Endpoints ───────────────────────────────────────────────

@router.post("/upload", status_code=201, response_model=FileOut)
async def upload_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = FastAPIFile(...),
    project_id: int = Form(...),
    session_id: Optional[str] = Form(None),
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload a file. Supported types are ingested for RAG; others → failed."""
    # Validate filename
    if not file.filename or len(file.filename) > 512:
        raise HTTPException(status_code=400, detail="Invalid filename")

    # Project access check
    if not await _can_user_access_project(db, user_id, project_id):
        raise HTTPException(status_code=403, detail="Access denied")

    # Size check BEFORE reading content.
    # UploadFile.size is set when Content-Length was provided.
    if file.size is not None and file.size > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (max {MAX_FILE_SIZE_MB} MB)",
        )

    # Streaming read with running size check (protects against missing CL header)
    content_parts = []
    total = 0
    CHUNK = 1024 * 1024  # 1 MB
    while True:
        chunk = await file.read(CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File too large (max {MAX_FILE_SIZE_MB} MB)",
            )
        content_parts.append(chunk)
    content = b"".join(content_parts)

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    file_hash = hashlib.sha256(content).hexdigest()

    # Dedup
    result = await db.execute(
        select(File).where(
            File.file_hash == file_hash,
            File.project_id == project_id,
            File.process_status == "ready",
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        return existing

    # Build object name (prevent path traversal via filename)
    safe_filename = file.filename.replace("/", "_").replace("\\", "_")[:512]
    sid = UUID(session_id) if session_id else None
    if project_id < 0:
        # Incognito — ephemeral, tied to session UUID
        object_name = f"incognito/{sid or 'no_session'}/{file_hash}_{safe_filename}"
    elif project_id == 0:
        # Base knowledge — shared across all users. Path scoped per uploader
        # so we can track who contributed what, but everyone can READ from project 0.
        object_name = f"base/user_{user_id}/{file_hash}_{safe_filename}"
    else:
        # Personal project
        object_name = f"user_{user_id}/project_{project_id}/{file_hash}_{safe_filename}"

    try:
        upload_to_minio(
            content,
            object_name,
            file.content_type or "application/octet-stream",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to store file: {e}")

    initial_status = "pending" if ext in SUPPORTED_EXTENSIONS else "failed"

    file_record = File(
        user_id=user_id,
        project_id=project_id,
        session_id=sid,
        file_hash=file_hash,
        file_name=safe_filename,
        file_type=ext or "unknown",
        process_status=initial_status,
        file_dir=f"{settings.MINIO_BUCKET}/{object_name}",
    )
    if initial_status == "failed":
        file_record.process_at = datetime.now(timezone.utc)

    db.add(file_record)
    await db.flush()
    await db.refresh(file_record)

    if initial_status == "pending":
        background_tasks.add_task(
            _run_ingest_background,
            str(file_record.file_id), content, safe_filename, ext, project_id,
        )

    return file_record


@router.get("", response_model=list[FileOut])
async def list_files(
    project_id: Optional[int] = None,
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if project_id is not None:
        if not await _can_user_access_project(db, user_id, project_id):
            raise HTTPException(status_code=403, detail="Access denied")
        query = select(File).where(
            File.project_id == project_id,
            File.process_status != "deleted",
        )
    elif user_id == 0:
        return []
    else:
        query = select(File).where(
            File.user_id == user_id,
            File.process_status != "deleted",
        )
    result = await db.execute(query.order_by(File.file_name))
    return result.scalars().all()


@router.get("/status/{file_id}", response_model=FileOut)
async def file_status(
    file_id: UUID,
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get file processing status. Authorized by project ownership."""
    return await _authorize_file(db, user_id, file_id)


@router.post("/reprocess/{file_id}", response_model=FileOut)
async def reprocess_file(
    file_id: UUID,
    background_tasks: BackgroundTasks,
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Re-process a failed file. Only works for supported formats."""
    f = await _authorize_file(db, user_id, file_id)

    if f.file_type not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400, detail=f"Unsupported type: {f.file_type}"
        )

    object_name = f.file_dir.replace(f"{settings.MINIO_BUCKET}/", "", 1)
    try:
        content = download_from_minio(object_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot read file: {e}")

    try:
        delete_file_chunks(str(file_id))
    except Exception:
        pass

    f.process_status = "pending"
    await db.flush()
    await db.refresh(f)

    background_tasks.add_task(
        _run_ingest_background,
        str(file_id), content, f.file_name, f.file_type, f.project_id,
    )
    return f


@router.delete("/{file_id}", status_code=204)
async def delete_file(
    file_id: UUID,
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    f = await _authorize_file(db, user_id, file_id)
    try:
        delete_file_chunks(str(file_id))
    except Exception:
        pass
    f.process_status = "deleted"
    f.process_at = datetime.now(timezone.utc)
