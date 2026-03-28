"""BDD test configuration — in-memory async SQLite database for real service testing.

Uses a real database so that SQL queries, foreign keys, and ownership filters
actually execute. No fakes, no mocks for the service layer.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import JSON, Boolean, Column, DateTime, String, func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


class ChatSessionTable(Base):
    """Minimal SQLite-compatible mirror of ChatSession for BDD tests."""

    __tablename__ = "chat_sessions"

    session_id = Column(String(36), primary_key=True)
    user_id = Column(String(36), nullable=False, index=True)
    organisation_id = Column(String(36), nullable=False, index=True)
    title = Column(String(255), nullable=False, default="Untitled Chat")
    chat_type = Column(String(50), nullable=False, default="GENERAL_CHAT")
    chat_version = Column(String(50), nullable=False, default="v1")
    properties = Column(JSON, nullable=True, default=dict)
    tags = Column(JSON, nullable=True, default=list)
    is_deleted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=func.now())
    updated_at = Column(DateTime, nullable=False, default=func.now(), onupdate=func.now())


@pytest.fixture(scope="session")
def event_loop():
    """Single event loop for the entire test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def async_engine(event_loop):
    """Create an async in-memory SQLite engine for BDD tests."""
    engine = event_loop.run_until_complete(_create_engine())
    yield engine
    event_loop.run_until_complete(engine.dispose())


async def _create_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


@pytest.fixture
def db_session(async_engine, event_loop):
    """Per-test sync-accessible async database session with rollback cleanup."""
    async_session_factory = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    session = event_loop.run_until_complete(async_session_factory().__aenter__())
    yield session
    event_loop.run_until_complete(session.rollback())
    event_loop.run_until_complete(session.__aexit__(None, None, None))
