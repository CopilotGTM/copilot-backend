from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.sql.dml import Delete
from src.domain.models.organisation_tables.sync.crm import CRMNote, CRMTask
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService


class _FakeSession:
    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, statement):  # noqa: ANN001
        self.statements.append(statement)


def _compiled_values(statement: object) -> dict[str, object]:
    return statement.compile().params


@pytest.mark.asyncio
async def test_task_projection_uses_source_id_payload_and_assignee_actor() -> None:
    organisation_id = uuid4()
    task_id = uuid4()
    integration_id = uuid4()
    contact_id = uuid4()
    company_id = uuid4()
    task = CRMTask(
        task_id=task_id,
        organisation_id=organisation_id,
        integration_id=integration_id,
        title="Follow up",
        description="Call buyer",
        status="IN_PROGRESS",
        priority="HIGH",
        completed_at=datetime(2026, 6, 22, 10, 30, tzinfo=UTC),
        assigned_to_name="Alex Morgan",
        assigned_to_email="alex@example.com",
        task_metadata={
            "provider": "salesforce",
            "task_type": "TASK",
            "contact_id": str(contact_id),
            "company_id": str(company_id),
        },
    )
    session = _FakeSession()

    await ActivityFeedProjectionService(session, organisation_id).upsert_task(task)

    values = _compiled_values(session.statements[0])
    assert values["activity_id"] == task_id
    assert values["source_record_id"] == str(task_id)
    assert values["source_provider"] == "salesforce"
    assert values["actor_name"] == "Alex Morgan"
    assert values["actor_email"] == "alex@example.com"
    assert values["display_payload"] == {
        "status": "IN_PROGRESS",
        "priority": "HIGH",
        "task_type": "TASK",
        "completed_at": "2026-06-22T10:30:00+00:00",
    }

    link_values = [
        _compiled_values(statement)
        for statement in session.statements
        if "crm_activity_links" in str(statement) and not isinstance(statement, Delete)
    ]
    assert {
        (values["object_type"], values["object_id"], values["link_role"], values["link_source"])
        for values in link_values
    } == {
        ("CONTACT", contact_id, "RELATED", "SYSTEM"),
        ("COMPANY", company_id, "RELATED", "SYSTEM"),
    }


@pytest.mark.asyncio
async def test_task_projection_removes_stale_contact_and_company_links() -> None:
    organisation_id = uuid4()
    task_id = uuid4()
    task = CRMTask(
        task_id=task_id,
        organisation_id=organisation_id,
        integration_id=uuid4(),
        title="Follow up",
        task_metadata={"provider": "salesforce"},
    )
    session = _FakeSession()

    await ActivityFeedProjectionService(session, organisation_id).upsert_task(task)

    delete_params = [
        _compiled_values(statement)
        for statement in session.statements
        if isinstance(statement, Delete)
    ]
    assert any("CONTACT" in params.values() for params in delete_params)
    assert any("COMPANY" in params.values() for params in delete_params)


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

    insert_values = _compiled_values(session.statements[0])
    assert insert_values["actor_name"] == "Workflow Bot"
    assert insert_values["actor_email"] == "bot@example.com"


@pytest.mark.asyncio
async def test_note_projection_uses_source_id_payload_provider_and_related_links() -> None:
    organisation_id = uuid4()
    note_id = uuid4()
    integration_id = uuid4()
    contact_id = uuid4()
    company_id = uuid4()
    note = CRMNote(
        note_id=note_id,
        organisation_id=organisation_id,
        integration_id=integration_id,
        title="Buyer note",
        body="Spoke with buyer offline",
        note_metadata={
            "provider": "salesforce",
            "contact_id": str(contact_id),
            "company_id": str(company_id),
        },
    )
    session = _FakeSession()

    await ActivityFeedProjectionService(session, organisation_id).upsert_note(note)

    values = _compiled_values(session.statements[0])
    assert values["source_record_id"] == str(note_id)
    assert values["source_provider"] == "salesforce"
    assert values["display_payload"] == {
        "body_format": "plain_text",
        "visibility": "TEAM",
        "has_attachments": False,
    }

    link_values = [
        _compiled_values(statement)
        for statement in session.statements
        if "crm_activity_links" in str(statement) and not isinstance(statement, Delete)
    ]
    assert {
        (values["object_type"], values["object_id"], values["link_role"], values["link_source"])
        for values in link_values
    } == {
        ("CONTACT", contact_id, "RELATED", "SYSTEM"),
        ("COMPANY", company_id, "RELATED", "SYSTEM"),
    }
