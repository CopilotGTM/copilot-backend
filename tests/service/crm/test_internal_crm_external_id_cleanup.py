"""Regression tests for local task/note writes and assignee notification hooks."""

from __future__ import annotations

from datetime import UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivityStatus,
)
from src.service.crm.notes.contracts import NoteWriteContext
from src.service.crm.notes.write_service import NotesWriteService
from src.service.crm.tasks.write_service import TaskWriteService


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.refreshed: list[object] = []
        self.executed: list[object] = []

    def add(self, obj: object) -> None:
        """Capture rows added by write services without needing a database."""
        self.added.append(obj)

    async def flush(self) -> None:
        """Match AsyncSession.flush for unit tests that do not persist rows."""
        return None

    async def refresh(self, obj: object) -> None:
        """Record refresh calls so tests can assert the write reached persistence."""
        self.refreshed.append(obj)

    async def execute(self, stmt: object) -> None:
        """Capture projection SQL issued by services under test."""
        self.executed.append(stmt)


@pytest.mark.asyncio
async def test_user_created_task_uses_local_ids_without_fake_external_ids() -> None:
    """User-created tasks should store local IDs and queue notifications via the service."""
    session = _FakeSession()
    organisation_id = uuid4()
    actor_user_id = uuid4()
    owner_crm_user_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Acme",
    )
    service = TaskWriteService(session, organisation_id, actor_user_id)
    service._load_deal = AsyncMock(return_value=deal)  # type: ignore[method-assign]
    service._resolve_owner = AsyncMock(  # type: ignore[method-assign]
        return_value=(owner_crm_user_id, "Owner", "owner@example.com")
    )
    service._notifications = SimpleNamespace(  # type: ignore[assignment]
        validate_external_assignee_domain=AsyncMock(),
        send_assignment_email=AsyncMock(),
    )

    task = await service.create_task(
        {
            "subject": "Follow up",
            "status": CRMActivityStatus.NOT_STARTED.value,
            "owner_user_id": actor_user_id,
        },
        deal_id=deal.deal_id,
    )

    assert not hasattr(task, "external_id")
    assert task.deal_id == deal.deal_id
    assert task.title == "Follow up"
    assert task.assigned_to_user_id == owner_crm_user_id
    assert task.owner_user_id == owner_crm_user_id
    assert not hasattr(task, "owner_external_id")


@pytest.mark.asyncio
async def test_user_created_task_notifies_internal_assignee() -> None:
    """Creating an assigned task should queue and then send an internal assignee email."""
    # Assignment notifications belong at the canonical write service so API,
    # bot form, and agent task creation all share the same side effect.
    session = _FakeSession()
    organisation_id = uuid4()
    actor_user_id = uuid4()
    owner_crm_user_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Acme",
    )
    notifications = SimpleNamespace(
        validate_external_assignee_domain=AsyncMock(),
        send_assignment_email=AsyncMock(),
    )
    service = TaskWriteService(session, organisation_id, actor_user_id)
    service._load_deal = AsyncMock(return_value=deal)  # type: ignore[method-assign]
    service._resolve_owner = AsyncMock(  # type: ignore[method-assign]
        return_value=(owner_crm_user_id, "Owner", "owner@example.com")
    )
    service._notifications = notifications  # type: ignore[assignment]

    task = await service.create_task(
        {
            "subject": "Follow up",
            "status": CRMActivityStatus.NOT_STARTED.value,
            "owner_user_id": actor_user_id,
        },
        deal_id=deal.deal_id,
    )

    notifications.send_assignment_email.assert_not_awaited()
    await service.send_pending_assignment_notifications()
    notifications.send_assignment_email.assert_awaited_once_with(
        task,
        deal,
        is_update=False,
    )


@pytest.mark.asyncio
async def test_user_task_reassignment_notifies_new_internal_assignee_as_assignment() -> None:
    """Reassigning a task should use assignment wording for the new assignee."""
    session = _FakeSession()
    organisation_id = uuid4()
    actor_user_id = uuid4()
    old_crm_user_id = uuid4()
    new_public_user_id = uuid4()
    new_crm_user_id = uuid4()
    task = SimpleNamespace(
        task_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=None,
        is_deleted=False,
        deal_id=None,
        title="Follow up",
        description=None,
        status="NOT_STARTED",
        priority="NONE",
        due_date=None,
        due_at=None,
        is_all_day=False,
        reminder_at=None,
        completed_at=None,
        completed_by_user_id=None,
        assigned_to_user_id=old_crm_user_id,
        assigned_to_name="Old Owner",
        assigned_to_email="old@example.com",
        owner_user_id=old_crm_user_id,
        owner_email="old@example.com",
        task_metadata={"assigned_to_public_user_id": str(actor_user_id)},
        source_modified_at=None,
    )
    notifications = SimpleNamespace(
        validate_external_assignee_domain=AsyncMock(),
        send_assignment_email=AsyncMock(),
    )
    service = TaskWriteService(session, organisation_id, actor_user_id)
    service._load_user_task = AsyncMock(return_value=task)  # type: ignore[method-assign]
    service._resolve_owner = AsyncMock(  # type: ignore[method-assign]
        return_value=(new_crm_user_id, "New Owner", "new@example.com")
    )
    service._notifications = notifications  # type: ignore[assignment]
    service._projection.upsert_task = AsyncMock()  # type: ignore[method-assign]

    updated = await service.update_task(task.task_id, {"owner_user_id": new_public_user_id})

    assert updated.assigned_to_email == "new@example.com"
    notifications.send_assignment_email.assert_not_awaited()
    await service.send_pending_assignment_notifications()
    notifications.send_assignment_email.assert_awaited_once_with(
        updated,
        None,
        is_update=False,
    )


@pytest.mark.asyncio
async def test_user_created_note_uses_local_ids_without_fake_external_ids() -> None:
    """User-created notes should keep provider/external IDs out of local source rows."""
    session = _FakeSession()
    organisation_id = uuid4()
    user_id = uuid4()
    deal_id = uuid4()
    deal = SimpleNamespace(
        deal_id=deal_id,
        owner_email=None,
        owner_name=None,
    )
    service = NotesWriteService(session, organisation_id)
    service._verify_deal_access = AsyncMock(return_value=deal)  # type: ignore[method-assign]
    service._find_existing_user_note = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await service.create_user_note(
        deal_id=deal_id,
        content="Useful note",
        subject=None,
        context=NoteWriteContext(
            source="api",
            actor_user_id=user_id,
            actor_name="Owner",
            actor_email="owner@example.com",
            created_via="api",
        ),
    )

    note = result.note
    assert result.created_new is True
    assert not hasattr(note, "external_id")
    assert note.deal_id == deal_id
    assert note.author_user_id is None
    assert not hasattr(note, "owner_external_id")
    assert note.note_metadata["created_by_public_user_id"] == str(user_id)
    assert note.source_created_at is not None
    assert note.source_created_at.tzinfo == UTC
