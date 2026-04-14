"""FinHouse — Files Router (upload, list, delete, status, re-process)."""

import hashlib
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File as FastAPIFile, Form, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, async_session_factory
from models import File
from routers.auth import get_current_user
from config import get_settings
from services.ingest import (
    SUPPORTED_EXTENSIONS,
    ingest_file,
    upload_to_minio,
    delete_file_chunks,
    download_from_minio,
)

router = APIRouter(prefix="/files", tags=["files"])
settings = get_settings()


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


# ── Background ingest task ──────────────────────────────────

async def _run_ingest_background(
    file_id: str, content: bytes, file_name: str, file_type: str, project_id: int
):
    """Background task: run full ingest pipeline then update DB status."""
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
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""

    content = await file.read()
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

    # MinIO path
    sid = UUID(session_id) if session_id else None
    if project_id < 0:
        object_name = f"incognito/{sid or 'no_session'}/{file_hash}_{file.filename}"
    else:
        object_name = f"user_{user_id}/project_{project_id}/{file_hash}_{file.filename}"

    try:
        upload_to_minio(content, object_name, file.content_type or "application/octet-stream")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to store file: {e}")

    initial_status = "pending" if ext in SUPPORTED_EXTENSIONS else "failed"

    file_record = File(
        user_id=user_id,
        project_id=project_id,
        session_id=sid,
        file_hash=file_hash,
        file_name=file.filename,
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
            str(file_record.file_id), content, file.filename, ext, project_id,
        )

    return file_record


@router.get("", response_model=list[FileOut])
async def list_files(
    project_id: Optional[int] = None,
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    query = select(File).where(File.process_status != "deleted")
    if project_id is not None:
        query = query.where(File.project_id == project_id)
    elif user_id != 0:
        query = query.where(File.user_id == user_id)
    result = await db.execute(query.order_by(File.file_name))
    return result.scalars().all()


@router.get("/status/{file_id}", response_model=FileOut)
async def file_status(file_id: UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(File).where(File.file_id == file_id))
    f = result.scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="File not found")
    return f


@router.post("/reprocess/{file_id}", response_model=FileOut)
async def reprocess_file(
    file_id: UUID,
    background_tasks: BackgroundTasks,
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Re-process a failed file. Only works for supported formats."""
    result = await db.execute(select(File).where(File.file_id == file_id))
    f = result.scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="File not found")
    if f.file_type not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported type: {f.file_type}")

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
    result = await db.execute(select(File).where(File.file_id == file_id))
    f = result.scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="File not found")
    try:
        delete_file_chunks(str(file_id))
    except Exception:
        pass
    f.process_status = "deleted"
    f.process_at = datetime.now(timezone.utc)
