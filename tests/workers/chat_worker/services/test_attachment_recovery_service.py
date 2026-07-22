"""Regression tests for durable chat-attachment post-upload recovery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from src.workers.chat_worker.services.stale_job_cleanup_service import (
    ATTACHMENT_PROCESSING_REQUEUE_AFTER_MINUTES,
    ATTACHMENT_RECOVERY_BATCH_SIZE,
    ChatStaleJobCleanupService,
)


class _Result:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        """Return the leased attachments through SQLAlchemy's scalar-result shape."""
        return SimpleNamespace(all=lambda: self.values)


class _LeaseSession:
    def __init__(self, attachments):
        self.attachments = attachments
        self.statement = None
        self.added = []
        self.commits = 0

    async def execute(self, statement):
        """Capture the lease query and return the configured attachments."""
        self.statement = statement
        return _Result(self.attachments)

    def add(self, value):
        """Record attachment updates staged by the recovery lease."""
        self.added.append(value)

    async def commit(self):
        """Record that the lease was persisted before task publication."""
        self.commits += 1


@pytest.mark.asyncio
async def test_stale_attachment_recovery_leases_a_bounded_processing_batch(monkeypatch) -> None:
    """Recovery must commit an org-scoped row lease before publishing to Redis."""
    organisation_id = uuid4()
    service = ChatStaleJobCleanupService(organisation_id)
    old_updated_at = datetime.now(UTC) - timedelta(minutes=20)
    attachment = SimpleNamespace(attachment_id=uuid4(), updated_at=old_updated_at)
    session = _LeaseSession([attachment])

    async def sessions(_organisation_id):
        """Yield the recording session through the production context-manager API."""
        yield session

    monkeypatch.setattr(
        "src.workers.chat_worker.services.stale_job_cleanup_service.acquire_org_async_session",
        sessions,
    )
    now = datetime.now(UTC)

    attachment_ids = await service._lease_stale_attachments(now)

    sql = str(
        session.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()
    assert "ATTACHMENTS.STATUS = 'PROCESSING'" in sql
    assert "ATTACHMENTS.IS_DELETED IS FALSE" in sql
    assert "ATTACHMENTS.STORAGE_PATH != ''" in sql
    assert f"LIMIT {ATTACHMENT_RECOVERY_BATCH_SIZE}" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert attachment_ids == [attachment.attachment_id]
    assert attachment.updated_at == now
    assert session.added == [attachment]
    assert session.commits == 1
    assert ATTACHMENT_PROCESSING_REQUEUE_AFTER_MINUTES == 12


@pytest.mark.asyncio
async def test_stale_attachment_recovery_keeps_publish_failures_retryable(monkeypatch) -> None:
    """One broker failure must not prevent other leased attachments from publishing."""
    from src.workers.chat_worker.services import stale_job_cleanup_service as recovery_module

    service = ChatStaleJobCleanupService(uuid4())
    attachment_ids = [uuid4(), uuid4()]
    monkeypatch.setattr(
        service,
        "_lease_stale_attachments",
        AsyncMock(return_value=attachment_ids),
    )
    publish = AsyncMock(side_effect=[RuntimeError("broker unavailable"), "task-2"])
    monkeypatch.setattr(recovery_module, "enqueue_attachment_post_upload", publish)

    counts = await service._recover_stale_attachments(datetime.now(UTC))

    assert counts == {"requeued": 1, "requeue_failed": 1}
    assert [call.args for call in publish.await_args_list] == [
        (service.organisation_id, attachment_ids[0]),
        (service.organisation_id, attachment_ids[1]),
    ]
