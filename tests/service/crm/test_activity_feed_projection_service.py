from __future__ import annotations

from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm import CRMNote, CRMTask
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService


class _FakeSession:
    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, statement):  # noqa: ANN001
        self.statements.append(statement)


@pytest.mark.asyncio
async def test_task_projection_does_not_treat_assignee_as_actor() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    task = CRMTask(
        organisation_id=organisation_id,
        integration_id=integration_id,
        title="Follow up",
        assigned_to_name="Alex Morgan",
        assigned_to_email="alex@example.com",
        task_metadata={
            "provider": "hubspot",
            "hs_task_type": "TODO",
        },
    )
    session = _FakeSession()

    await ActivityFeedProjectionService(session, organisation_id).upsert_task(task)

    insert_values = session.statements[0].compile().params
    assert insert_values["actor_name"] is None
    assert insert_values["actor_email"] is None
    assert insert_values["source_provider"] == "hubspot"
    assert insert_values["display_payload"]["task_type"] == "TODO"
    assert insert_values["display_payload"]["assignee_name"] == "Alex Morgan"
    assert insert_values["display_payload"]["assignee_email"] == "alex@example.com"


@pytest.mark.asyncio
async def test_task_projection_uses_explicit_actor_metadata() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    task = CRMTask(
        organisation_id=organisation_id,
        integration_id=integration_id,
        title="Follow up",
        assigned_to_name="Alex Morgan",
        assigned_to_email="alex@example.com",
        task_metadata={
            "actor_name": "Workflow Bot",
            "actor_email": "bot@example.com",
        },
    )
    session = _FakeSession()

    await ActivityFeedProjectionService(session, organisation_id).upsert_task(task)

    insert_values = session.statements[0].compile().params
    assert insert_values["actor_name"] == "Workflow Bot"
    assert insert_values["actor_email"] == "bot@example.com"


@pytest.mark.asyncio
async def test_note_projection_uses_metadata_provider() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    note = CRMNote(
        organisation_id=organisation_id,
        integration_id=integration_id,
        title="Buyer note",
        body="Spoke with buyer offline",
        note_metadata={
            "provider": "hubspot",
            "hs_attachment_ids": ["attachment-1"],
        },
    )
    session = _FakeSession()

    await ActivityFeedProjectionService(session, organisation_id).upsert_note(note)

    insert_values = session.statements[0].compile().params
    assert insert_values["source_provider"] == "hubspot"
    assert insert_values["display_payload"]["has_attachments"] is True
