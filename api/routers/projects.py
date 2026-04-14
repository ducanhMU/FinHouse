"""FinHouse — Projects Router."""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Project
from routers.auth import get_current_user

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    project_title: str
    description: Optional[str] = None

class ProjectUpdate(BaseModel):
    project_title: Optional[str] = None
    description: Optional[str] = None

class ProjectOut(BaseModel):
    project_id: int
    user_id: int
    project_title: str
    description: Optional[str]
    create_at: datetime
    update_at: datetime

    class Config:
        from_attributes = True


@router.post("", status_code=201, response_model=ProjectOut)
async def create_project(
    body: ProjectCreate,
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user_id == 0:
        raise HTTPException(status_code=401, detail="Authentication required")

    result = await db.execute(text("SELECT nextval('project_id_seq')"))
    new_id = result.scalar()

    project = Project(
        project_id=new_id,
        user_id=user_id,
        project_title=body.project_title,
        description=body.description,
    )
    db.add(project)
    await db.flush()
    await db.refresh(project)
    return project


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user_id == 0:
        return []

    result = await db.execute(
        select(Project)
        .where(Project.user_id == user_id, Project.project_id >= 0)
        .order_by(Project.update_at.desc())
    )
    return result.scalars().all()


@router.put("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: int,
    body: ProjectUpdate,
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user_id == 0:
        raise HTTPException(status_code=401, detail="Authentication required")

    result = await db.execute(
        select(Project).where(
            Project.project_id == project_id, Project.user_id == user_id
        )
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if body.project_title is not None:
        project.project_title = body.project_title
    if body.description is not None:
        project.description = body.description
    project.update_at = datetime.now(timezone.utc)
    await db.flush()
    await db.refresh(project)
    return project


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    project_id: int,
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user_id == 0:
        raise HTTPException(status_code=401, detail="Authentication required")
    if project_id <= 0:
        raise HTTPException(status_code=400, detail="Cannot delete default/system project")

    result = await db.execute(
        select(Project).where(
            Project.project_id == project_id, Project.user_id == user_id
        )
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await db.delete(project)
