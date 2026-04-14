"""FinHouse — SQLAlchemy ORM Models."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, ForeignKey, ARRAY, Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "user"

    user_id = Column(Integer, primary_key=True)
    user_name = Column(String(128), unique=True, nullable=False)
    user_password = Column(String(256), nullable=True)
    create_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    projects = relationship("Project", back_populates="owner")


class Project(Base):
    __tablename__ = "project"

    project_id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("user.user_id"), nullable=False)
    project_title = Column(String(256), nullable=False)
    description = Column(Text, nullable=True)
    create_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    update_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    owner = relationship("User", back_populates="projects")
    sessions = relationship(
        "ChatSession", back_populates="project", cascade="all, delete-orphan"
    )
    files = relationship(
        "File", back_populates="project", cascade="all, delete-orphan"
    )


class ChatSession(Base):
    __tablename__ = "chat_session"

    session_id = Column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id = Column(
        Integer, ForeignKey("project.project_id", ondelete="CASCADE"), nullable=False
    )
    session_title = Column(String(512), nullable=True)
    create_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    update_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    model_used = Column(String(128), nullable=False)
    tools_used = Column(ARRAY(Text), nullable=True)
    turn_count = Column(Integer, nullable=False, default=0)
    summary_count = Column(Integer, nullable=False, default=0)

    project = relationship("Project", back_populates="sessions")
    events = relationship(
        "ChatEvent", back_populates="session", cascade="all, delete-orphan"
    )


class ChatEvent(Base):
    __tablename__ = "chat_event"

    message_id = Column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("chat_session.session_id", ondelete="CASCADE"),
        nullable=False,
    )
    num_order = Column(Integer, nullable=False)
    role = Column(String(32), nullable=False)
    text = Column(Text, nullable=False)
    event_type = Column(String(32), nullable=False)
    create_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    session = relationship("ChatSession", back_populates="events")


class File(Base):
    __tablename__ = "file"

    file_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(Integer, ForeignKey("user.user_id"), nullable=False)
    project_id = Column(
        Integer, ForeignKey("project.project_id", ondelete="CASCADE"), nullable=False
    )
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("chat_session.session_id", ondelete="SET NULL"),
        nullable=True,
    )
    file_hash = Column(String(64), nullable=False)
    file_name = Column(String(512), nullable=False)
    file_type = Column(String(16), nullable=False)
    process_status = Column(String(32), nullable=False, default="pending")
    process_at = Column(DateTime(timezone=True), nullable=True)
    file_dir = Column(String(1024), nullable=False)

    project = relationship("Project", back_populates="files")
