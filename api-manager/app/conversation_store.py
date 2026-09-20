"""Persistent conversation metadata; task execution remains file-backed."""

import os
from datetime import datetime, timezone
from functools import lru_cache

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    repository_url: Mapped[str] = mapped_column(Text, nullable=False)
    repository_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    repository_refresh: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (Index("ix_conversations_user_updated", "user_id", "updated_at"),)


class Turn(Base):
    __tablename__ = "turns"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    assistant_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="starting")
    context_rounds_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uq_turn_sequence"),
        UniqueConstraint("conversation_id", "request_id", name="uq_turn_request_id"),
        Index("ix_turns_conversation_sequence", "conversation_id", "sequence"),
    )


class UserMcpInstallation(Base):
    __tablename__ = "user_mcp_installations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    mcp_id: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "mcp_id", name="uq_user_mcp_installation"),
        Index("ix_user_mcp_installations_user", "user_id"),
    )


@lru_cache(maxsize=1)
def session_factory():
    url = os.environ.get("DATABASE_URL", "sqlite:////data/conversations.sqlite3")
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite:"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
