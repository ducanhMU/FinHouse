"""FinHouse — Sessions Router."""

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import ChatSession, Project
from routers.auth import get_current_user

router = APIRouter(prefix="/sessions", tags=["sessions"])


class SessionCreate(BaseModel):
    project_id: Optional[int] = None
    model_used: str
    tools_used: Optional[list[str]] = None

class SessionUpdate(BaseModel):
    session_title: Optional[str] = None

class SessionOut(BaseModel):
    session_id: UUID
    project_id: int
    session_title: Optional[str]
    create_at: datetime
    update_at: datetime
    model_used: str
    tools_used: Optional[list[str]]
    turn_count: int
    summary_count: int

    class Config:
        from_attributes = True


@router.post("", status_code=201, response_model=SessionOut)
async def create_session(
    body: SessionCreate,
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project_id = body.project_id

    if user_id == 0 or project_id is None and user_id == 0:
        # Guest / incognito → create temp negative project
        result = await db.execute(text("SELECT nextval('incognito_project_seq')"))
        neg_id = result.scalar()
        project = Project(
            project_id=neg_id,
            user_id=0,
            project_title="Incognito Session",
        )
        db.add(project)
        await db.flush()
        project_id = neg_id

    elif project_id is None:
        # Authenticated, no project selected → default inbox
        project_id = 0
        # Ensure project 0 is owned by or accessible to this user
        # (project 0 is the shared default — anyone can use it)

    else:
        # Validate project ownership
        result = await db.execute(
            select(Project).where(Project.project_id == project_id)
        )
        proj = result.scalar_one_or_none()
        if not proj:
            raise HTTPException(status_code=404, detail="Project not found")
        if proj.user_id != user_id and proj.user_id != 0:
            raise HTTPException(status_code=403, detail="Not your project")

    session = ChatSession(
        project_id=project_id,
        model_used=body.model_used,
        tools_used=body.tools_used,
    )
    db.add(session)
    await db.flush()
    await db.refresh(session)
    return session


@router.get("", response_model=list[SessionOut])
async def list_sessions(
    project_id: Optional[int] = None,
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user_id == 0:
        return []

    query = (
        select(ChatSession)
        .join(Project, ChatSession.project_id == Project.project_id)
        .where(Project.user_id == user_id, Project.project_id >= 0)
    )
    if project_id is not None:
        query = query.where(ChatSession.project_id == project_id)

    query = query.order_by(ChatSession.update_at.desc())
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{session_id}", response_model=SessionOut)
async def get_session(
    session_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ChatSession).where(ChatSession.session_id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.put("/{session_id}", response_model=SessionOut)
async def update_session(
    session_id: UUID,
    body: SessionUpdate,
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ChatSession).where(ChatSession.session_id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if body.session_title is not None:
        session.session_title = body.session_title
    session.update_at = datetime.now(timezone.utc)
    await db.flush()
    await db.refresh(session)
    return session


@router.delete("/{session_id}", status_code=204)
async def delete_session(
    session_id: UUID,
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ChatSession).where(ChatSession.session_id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    await db.delete(session)
