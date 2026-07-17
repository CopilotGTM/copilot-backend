"""Ownership contracts for transient chat job event streams."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.job_models import JobStatus
from src.service.chat import chat_message_sse_service as sse_module
from src.service.chat.chat_message_sse_service import ChatMessageSSEService


def _patch_job_lookup(monkeypatch, job) -> None:
    session = AsyncMock()
    session.get.return_value = job

    async def sessions(_organisation_id):
        """Yield the controlled database response for one ownership lookup."""
        yield session

    monkeypatch.setattr(sse_module, "acquire_org_async_session", sessions)


@pytest.mark.asyncio
async def test_can_stream_rejects_non_owner(monkeypatch) -> None:
    """Only the job owner may consume transient model frames."""
    service = ChatMessageSSEService(uuid4(), uuid4(), uuid4())
    _patch_job_lookup(monkeypatch, SimpleNamespace(user_id=uuid4(), status=JobStatus.PENDING))

    assert await service.can_stream() is False


@pytest.mark.asyncio
async def test_can_stream_accepts_owner(monkeypatch) -> None:
    """A matching job owner retains the existing SSE behavior."""
    user_id = uuid4()
    service = ChatMessageSSEService(uuid4(), uuid4(), user_id)
    _patch_job_lookup(monkeypatch, SimpleNamespace(user_id=user_id, status=JobStatus.PENDING))

    assert await service.can_stream() is True


@pytest.mark.asyncio
async def test_can_stream_rejects_missing_job(monkeypatch) -> None:
    """Unknown job IDs remain indistinguishable from another user's jobs."""
    service = ChatMessageSSEService(uuid4(), uuid4(), uuid4())
    _patch_job_lookup(monkeypatch, None)

    assert await service.can_stream() is False


@pytest.mark.asyncio
async def test_completed_status_uses_owned_job_lookup(monkeypatch) -> None:
    """Completion checks must not treat another user's job as streamable."""
    user_id = uuid4()
    service = ChatMessageSSEService(uuid4(), uuid4(), user_id)
    _patch_job_lookup(monkeypatch, SimpleNamespace(user_id=user_id, status=JobStatus.COMPLETED))

    assert await service._is_job_completed() is True
