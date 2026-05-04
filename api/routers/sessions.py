"""FinHouse — Sessions Router."""

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import ChatSession, Project
from routers.auth import get_current_user

router = APIRouter(prefix="/sessions", tags=["sessions"])


class SessionCreate(BaseModel):
    project_id: Optional[int] = None
    # Optional — defaults to settings.DEFAULT_MODEL. The "session model"
    # is now only a fallback for any agent whose *_AGENT_LLM env var is
    # empty; the per-agent brains override it. UI no longer exposes a
    # picker — the backend fills this in.
    model_used: Optional[str] = None
    # Ignored by the server — every session auto-enables every tool the
    # deployment supports. Kept in the schema for back-compat with
    # older clients but not honored. The orchestrator decides which
    # tool to actually invoke per-turn based on user intent.
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


# ════════════════════════════════════════════════════════════
# Authorization helper (used by chat, sessions, files routers)
# ════════════════════════════════════════════════════════════

async def _can_user_access_project(
    db: AsyncSession, user_id: int, project_id: int
) -> bool:
    """
    Returns True if the given user can access the given project.
    Rules:
      - Guest (user_id=0) can only access project 0 (shared) and negative
        project IDs (incognito — accessible by anyone who knows the ID).
      - Authenticated user can access their own projects + project 0.
      - Negative project IDs are temporary/incognito; any holder is allowed.
    """
    # Incognito / negative → anyone who knows the ID can access
    if project_id < 0:
        return True

    # Default inbox (project 0) — available to all authenticated users
    # and to guests. Not truly "public" since it requires auth to create
    # sessions here, and each session's events are tied to specific user
    # via session-level ownership (see authorize_session below).
    if project_id == 0:
        return True

    # Guests cannot access non-zero, non-negative projects
    if user_id == 0:
        return False

    # Auth user — must own the project
    result = await db.execute(
        select(Project.user_id).where(Project.project_id == project_id)
    )
    owner = result.scalar_one_or_none()
    if owner is None:
        return False
    return owner == user_id


async def authorize_session(
    db: AsyncSession, user_id: int, session_id: UUID
) -> ChatSession:
    """
    Load a session and verify the user can access it.
    Returns the session, or raises 404/403.

    Session access rule: the user must be able to access the session's
    project. Sessions within project 0 additionally require the user to
    have participated (i.e. created it) — we approximate this by treating
    all authenticated access to project 0 as permitted but guests can
    ONLY access sessions they created. Since we don't track creator on
    the session row, guests get no access to project-0 sessions.
    """
    result = await db.execute(
        select(ChatSession).where(ChatSession.session_id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Project-level access check
    if not await _can_user_access_project(db, user_id, session.project_id):
        raise HTTPException(status_code=403, detail="Access denied")

    # Guests cannot read sessions in project 0 — too leaky (anyone knowing
    # UUIDs could read sessions from other guests).
    if user_id == 0 and session.project_id == 0:
        raise HTTPException(status_code=403, detail="Access denied")

    return session


# ════════════════════════════════════════════════════════════
# Endpoints
# ════════════════════════════════════════════════════════════

@router.post("", status_code=201, response_model=SessionOut)
async def create_session(
    body: SessionCreate,
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project_id = body.project_id

    if user_id == 0 and project_id is None:
        # Guest without explicit project → always a temp incognito project
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
        # Authenticated, no project selected → default inbox (project 0)
        project_id = 0

    else:
        # Explicit project_id — check access
        if not await _can_user_access_project(db, user_id, project_id):
            raise HTTPException(status_code=403, detail="Access denied")

    # Auto-enable every tool the deployment supports. The orchestrator
    # picks which to actually use per-turn based on user intent — and
    # the collector suggests unused-but-relevant tools at the end of
    # each answer. We ignore any client-supplied tools_used list to
    # keep agent capability uniform across sessions.
    from config import get_settings
    _s = get_settings()
    tools = ["web_search"]
    if _s.CLICKHOUSE_HOST:
        tools.extend(["database_query", "visualize"])

    session = ChatSession(
        project_id=project_id,
        model_used=(body.model_used or _s.DEFAULT_MODEL or "qwen2.5:14b"),
        tools_used=tools,
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
        # Verify user can see this specific project
        if not await _can_user_access_project(db, user_id, project_id):
            raise HTTPException(status_code=403, detail="Access denied")
        query = query.where(ChatSession.project_id == project_id)

    query = query.order_by(ChatSession.update_at.desc())
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{session_id}", response_model=SessionOut)
async def get_session(
    session_id: UUID,
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await authorize_session(db, user_id, session_id)


@router.put("/{session_id}", response_model=SessionOut)
async def update_session(
    session_id: UUID,
    body: SessionUpdate,
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = await authorize_session(db, user_id, session_id)

    if body.session_title is not None:
        # Cap title length
        session.session_title = body.session_title[:512]
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
    session = await authorize_session(db, user_id, session_id)
    await db.delete(session)
