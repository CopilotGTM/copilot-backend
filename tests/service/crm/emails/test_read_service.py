"""Tests for read-only CRM email sidebar queries."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.service.crm.emails.read_service import CRMEmailReadService, CRMEmailScope


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _ScalarResult(self._rows)


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def execute(self, statement):
        self.statements.append(str(statement))
        return _Result(self.rows)

    async def close(self):
        pass


def _email(*, received_at: datetime, email_id=None):
    return SimpleNamespace(
        email_id=email_id or uuid4(),
        integration_id=uuid4(),
        internet_message_id="<message@example.com>",
        thread_id="thread-1",
        subject="Quarterly review",
        from_name="Buyer",
        from_email="buyer@example.com",
        to_emails=["seller@example.com"],
        cc_emails=[],
        received_at=received_at,
        sent_at=None,
        direction="inbound",
        is_read=False,
        is_important=True,
        has_attachments=True,
        labels=["Inbox"],
        snippet="A short preview",
        body_text="x" * 1100,
    )


@pytest.mark.asyncio
async def test_list_emails_returns_bounded_sidebar_cards_and_next_cursor():
    newer = _email(received_at=datetime(2026, 7, 26, tzinfo=UTC))
    older = _email(received_at=datetime(2026, 7, 25, tzinfo=UTC))
    service = CRMEmailReadService(_Session([newer, older]), uuid4(), uuid4())

    page = await service.list_emails(scope=CRMEmailScope.OVERALL, limit=1)

    assert len(page.items) == 1
    assert page.cursor
    assert page.items[0].email_id == newer.email_id
    assert page.items[0].body_preview == "x" * 1000
    assert "body_html" not in type(page.items[0]).model_fields


@pytest.mark.asyncio
async def test_list_emails_rejects_an_invalid_cursor():
    service = CRMEmailReadService(_Session([]), uuid4(), uuid4())

    with pytest.raises(ValueError, match="Invalid cursor"):
        await service.list_emails(scope=CRMEmailScope.OVERALL, cursor="not-a-cursor")


@pytest.mark.asyncio
async def test_contact_scope_uses_activity_links_not_raw_email_addresses():
    session = _Session([])
    service = CRMEmailReadService(session, uuid4(), uuid4())

    await service.list_emails(scope=CRMEmailScope.CONTACT, object_id=uuid4())

    assert "crm_activity_links" in session.statements[0]
    assert "crm_activities" in session.statements[0]


@pytest.mark.parametrize("scope", [CRMEmailScope.CONTACT, CRMEmailScope.ACCOUNT])
@pytest.mark.asyncio
async def test_object_scope_maps_canonical_projection_to_callers_mailbox_copy(scope):
    session = _Session([])
    service = CRMEmailReadService(session, uuid4(), uuid4())

    await service.list_emails(scope=scope, object_id=uuid4())

    sql = session.statements[0]
    assert "canonical_emails.internet_message_id = emails.internet_message_id" in sql
    assert "crm_activities.source_record_id = CAST(canonical_emails.email_id AS VARCHAR)" in sql
    assert "knowledge_integrations.created_by_user_id" in sql


@pytest.mark.asyncio
async def test_deal_scope_uses_active_deal_email_associations():
    session = _Session([])
    service = CRMEmailReadService(session, uuid4(), uuid4())

    await service.list_emails(scope=CRMEmailScope.DEAL, object_id=uuid4())

    assert "deal_email_associations" in session.statements[0]
    assert "deal_email_associations.status" in session.statements[0]
