from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.sql.dml import Delete
from src.domain.models.organisation_tables.sync.email_models import Email, EmailDirection
from src.service.crm.activity_feed_projection_service import (
    ActivityFeedProjectionService,
    communication_source_activity_id,
)


class _ExecuteResult:
    def __init__(self, scalar: object | None = None) -> None:
        self.scalar = scalar

    def scalars(self):
        return self

    def all(self) -> list[object]:
        return []

    def scalar_one_or_none(self) -> object | None:
        return self.scalar


class _Session:
    def __init__(self, *, active_association_id: object | None = None) -> None:
        self.statements: list[object] = []
        self.active_association_id = active_association_id

    async def execute(self, statement, _parameters=None):
        self.statements.append(statement)
        if "deal_email_associations" in str(statement):
            return _ExecuteResult(self.active_association_id)
        return _ExecuteResult()


def _values(statement: object) -> dict[str, object]:
    return statement.compile().params


@pytest.mark.asyncio
async def test_direct_email_is_one_card_with_all_contact_links() -> None:
    organisation_id = uuid4()
    email = Email(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        folder_id=uuid4(),
        email_id=uuid4(),
        external_message_id="gmail-message-1",
        internet_message_id="<shared-message@example.com>",
        from_email="buyer@example.com",
        from_name="Buyer",
        to_emails=["seller@ourco.com"],
        cc_emails=["champion@example.com"],
        subject="Commercials",
        snippet="Can you send pricing?",
        received_at=datetime(2026, 7, 2, 9, 30, tzinfo=UTC),
        direction=EmailDirection.INBOUND,
        has_attachments=False,
        email_metadata={"provider": "gmail"},
    )
    first_contact_id = uuid4()
    second_contact_id = uuid4()
    company_id = uuid4()
    session = _Session()

    projected = await ActivityFeedProjectionService(
        session,  # type: ignore[arg-type]
        organisation_id,
    ).upsert_direct_email(
        email,
        contact_ids={first_contact_id, second_contact_id},
        company_ids={company_id},
    )

    assert projected is True
    activity_statement = next(
        statement for statement in session.statements if "crm_activities" in str(statement)
    )
    activity = _values(activity_statement)
    assert activity["activity_id"] == communication_source_activity_id(
        organisation_id,
        "email",
        email.internet_message_id,
    )
    assert activity["source_record_id"] == str(email.email_id)
    link_values = [
        _values(statement)
        for statement in session.statements
        if "crm_activity_links" in str(statement) and not isinstance(statement, Delete)
    ]
    assert {
        (value["object_type"], value["object_id"], value["link_role"]) for value in link_values
    } == {
        ("CONTACT", first_contact_id, "RELATED"),
        ("CONTACT", second_contact_id, "RELATED"),
        ("COMPANY", company_id, "RELATED"),
    }


@pytest.mark.asyncio
async def test_direct_email_is_not_created_after_a_deal_association_exists() -> None:
    organisation_id = uuid4()
    email = Email(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        folder_id=uuid4(),
        email_id=uuid4(),
        external_message_id="gmail-message-2",
        internet_message_id="<associated-message@example.com>",
        from_email="buyer@example.com",
        to_emails=["seller@ourco.com"],
        subject="Already matched",
        received_at=datetime(2026, 7, 2, 9, 30, tzinfo=UTC),
        direction=EmailDirection.INBOUND,
    )
    session = _Session(active_association_id=uuid4())

    projected = await ActivityFeedProjectionService(
        session,  # type: ignore[arg-type]
        organisation_id,
    ).upsert_direct_email(email, contact_ids={uuid4()}, company_ids=set())

    assert projected is False
    assert not any("crm_activities" in str(statement) for statement in session.statements)


@pytest.mark.asyncio
async def test_association_participant_links_use_an_independent_role() -> None:
    organisation_id = uuid4()
    contact_id = uuid4()
    company_id = uuid4()
    session = _Session()

    await ActivityFeedProjectionService(
        session,  # type: ignore[arg-type]
        organisation_id,
    ).sync_communication_participant_links(
        activity_id=uuid4(),
        contact_ids={contact_id},
        company_ids={company_id},
        occurred_at=datetime(2026, 7, 2, 9, 30, tzinfo=UTC),
        replace_existing=False,
    )

    link_values = [
        _values(statement)
        for statement in session.statements
        if "crm_activity_links" in str(statement) and not isinstance(statement, Delete)
    ]
    assert {
        (value["object_type"], value["object_id"], value["link_role"]) for value in link_values
    } == {
        ("CONTACT", contact_id, "PARTICIPANT"),
        ("COMPANY", company_id, "PARTICIPANT"),
    }
    assert not any(isinstance(statement, Delete) for statement in session.statements)
