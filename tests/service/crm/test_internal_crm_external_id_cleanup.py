from __future__ import annotations

from datetime import UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivityObjectType,
    CRMActivityStatus,
)
from src.service.crm.internal.deal_notes_service import DealNotesService
from src.service.crm.internal.tasks_write_service import TasksWriteService


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.refreshed: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def refresh(self, obj: object) -> None:
        self.refreshed.append(obj)


@pytest.mark.asyncio
async def test_user_created_task_uses_local_ids_without_fake_external_ids() -> None:
    session = _FakeSession()
    organisation_id = uuid4()
    actor_user_id = uuid4()
    owner_crm_user_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Acme",
    )
    service = TasksWriteService(session, organisation_id, actor_user_id)
    service._load_deal = AsyncMock(return_value=deal)  # type: ignore[method-assign]
    service._resolve_owner = AsyncMock(  # type: ignore[method-assign]
        return_value=(owner_crm_user_id, "Owner", "owner@example.com")
    )
    service._send_external_assignment_email = AsyncMock()  # type: ignore[method-assign]

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
    assert task.related_object_type == CRMActivityObjectType.DEAL.value
    assert task.related_local_id == deal.deal_id
    assert task.owner_id == owner_crm_user_id
    assert not hasattr(task, "owner_external_id")


@pytest.mark.asyncio
async def test_user_created_note_uses_local_ids_without_fake_external_ids() -> None:
    session = _FakeSession()
    organisation_id = uuid4()
    user_id = uuid4()
    deal_id = uuid4()
    deal = SimpleNamespace(
        deal_id=deal_id,
        owner_email=None,
        owner_name=None,
    )
    service = DealNotesService(session, organisation_id)
    service._verify_deal_access = AsyncMock(return_value=deal)  # type: ignore[method-assign]
    service._find_existing_user_note = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await service.create_user_note(
        deal_id=deal_id,
        content="Useful note",
        subject=None,
        user_id=user_id,
        user_name="Owner",
        user_email="owner@example.com",
        created_via="api",
    )

    note = result.note
    assert result.created_new is True
    assert not hasattr(note, "external_id")
    assert note.deal_id == deal_id
    assert note.owner_id is None
    assert not hasattr(note, "owner_external_id")
    assert note.created_by_user_id == user_id
    assert note.source_created_at is not None
    assert note.source_created_at.tzinfo == UTC
