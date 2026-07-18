"""Contract tests for single-message and full-thread email detail reads."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import MultipleResultsFound
from src.agents.tools.communication.get_email_details import get_email_details
from src.agents.tools.crm.access import AgentCRMAccessError
from src.domain.models.organisation_tables.sync.calendar import AssociationStatus
from src.domain.models.organisation_tables.sync.email_models import (
    DealEmailDirection,
    EmailDirection,
    EmailPriority,
)
from src.domain.models.organisation_tables.sync.integration_models import IntegrationType
from src.infra.config.google_integration_scopes import GOOGLE_SCOPE_GMAIL_READONLY
from src.service.crm.access.record_access_policy import CRMSystemActor


class _FakeResult:
    def __init__(self, items):
        self._items = list(items)

    def scalar_one_or_none(self):
        """Raise on duplicates so tests catch accidental non-deterministic lookups."""
        if len(self._items) > 1:
            raise MultipleResultsFound("Multiple rows were found when one or none was required")
        return self._items[0] if self._items else None

    def scalars(self):
        """Mirror the SQLAlchemy scalar-result chain used by the tool."""
        return self

    def all(self):
        """Return the configured rows without database behavior."""
        return list(self._items)

    def first(self):
        """Return the deterministic first configured row."""
        return self._items[0] if self._items else None


class _FakeSession:
    def __init__(self, execute_results):
        self._execute_results = list(execute_results)
        self.statements = []

    async def execute(self, stmt):
        """Record each boundary query and return its deterministic rows."""
        self.statements.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
        return _FakeResult(self._execute_results.pop(0))


class _FakeContext:
    def __init__(self, organisation_id, session, user_id=None, deal_id=None):
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.deal_id = deal_id
        self.metadata = {}
        self._session = session

    @asynccontextmanager
    async def acquire_session(self):
        """Yield the fake org-scoped session expected by agent tools."""
        yield self._session


def _make_email(*, organisation_id, internet_message_id, received_at, subject, body_text, snippet):
    return SimpleNamespace(
        email_id=uuid4(),
        integration_id=uuid4(),
        organisation_id=organisation_id,
        internet_message_id=internet_message_id,
        thread_id="thread-1",
        subject=subject,
        from_email="sender@example.com",
        from_name="Sender",
        to_emails=["recipient@example.com"],
        cc_emails=[],
        received_at=received_at,
        sent_at=received_at,
        direction=EmailDirection.INBOUND,
        priority=EmailPriority.NORMAL,
        is_read=True,
        is_starred=False,
        is_important=False,
        has_attachments=False,
        labels=["inbox"],
        attachments=[],
        body_text=body_text,
        body_html=None,
        snippet=snippet,
    )


def _make_association(deal_id):
    return SimpleNamespace(
        deal_id=deal_id,
        direction=DealEmailDirection.INBOUND,
        status=AssociationStatus.ACTIVE,
        match_score=0.98,
        matched_stakeholder_emails=["buyer@example.com"],
    )


def _mailbox(integration_id):
    return SimpleNamespace(
        integration_id=integration_id,
        integration_type=IntegrationType.GOOGLE,
        is_active=True,
        requires_reauth=False,
        scopes=[GOOGLE_SCOPE_GMAIL_READONLY],
    )


@pytest.mark.asyncio
async def test_get_email_details_rejects_message_id_without_authorized_scope():
    """An RFC Message-ID is an identifier, never authority to read org-wide mail."""
    organisation_id = uuid4()
    session = _FakeSession([])
    context = _FakeContext(organisation_id=organisation_id, session=session)

    result = await get_email_details("<coworker@example.com>", context=context)

    assert result is None
    assert session.statements == []


@pytest.mark.asyncio
async def test_get_email_details_can_return_chronological_provider_thread(monkeypatch):
    """Thread reads stay within the selected mailbox integration and include history."""
    organisation_id = uuid4()
    integration_id = uuid4()
    user_id = uuid4()
    internet_message_id = "<latest@example.com>"
    older = _make_email(
        organisation_id=organisation_id,
        internet_message_id="<older@example.com>",
        received_at=datetime(2026, 7, 13, tzinfo=UTC),
        subject="Re: Evaluation",
        body_text="Earlier message",
        snippet="Earlier",
    )
    latest = _make_email(
        organisation_id=organisation_id,
        internet_message_id=internet_message_id,
        received_at=datetime(2026, 7, 14, tzinfo=UTC),
        subject="Re: Evaluation",
        body_text="Latest message",
        snippet="Latest",
    )
    older.integration_id = integration_id
    latest.integration_id = integration_id
    mailbox = _mailbox(integration_id)
    session = _FakeSession([[mailbox], [latest], [latest, older], []])
    context = _FakeContext(
        organisation_id,
        session,
        user_id=user_id,
    )

    async def _fake_actor(_context):
        return CRMSystemActor("test")

    monkeypatch.setattr(
        "src.agents.tools.communication.get_email_details.resolve_agent_access_actor",
        _fake_actor,
    )

    result = await get_email_details(
        internet_message_id,
        context=context,
        mailbox_email="recipient@example.com",
        include_thread=True,
    )

    assert result is not None
    assert [item["internet_message_id"] for item in result["thread_messages"]] == [
        "<older@example.com>",
        internet_message_id,
    ]
    assert result["thread_message_count"] == 2
    assert result["thread_truncated"] is False
    assert result["access_scope"] == "mailbox"
    assert "emails.integration_id =" in session.statements[1]
    assert "emails.is_deleted IS false" in session.statements[1]
    assert "emails.integration_id =" in session.statements[2]
    assert "deal_email_associations.status = 'ACTIVE'" in session.statements[3]
    assert "JOIN crm_deals" in session.statements[3]


@pytest.mark.asyncio
async def test_get_email_details_deal_scope_requires_access_and_active_association(
    monkeypatch,
):
    """Deal scope exposes only messages attached to the deal after its access guard."""
    organisation_id = uuid4()
    deal_id = uuid4()
    email = _make_email(
        organisation_id=organisation_id,
        internet_message_id="<deal@example.com>",
        received_at=datetime.now(UTC),
        subject="Negotiation",
        body_text="Please confirm.",
        snippet="Please confirm.",
    )
    association = _make_association(deal_id)
    session = _FakeSession([[email], [email], [association]])
    context = _FakeContext(organisation_id, session, user_id=uuid4(), deal_id=deal_id)

    async def _allow_deal(_context, _session, requested_deal_id):
        assert requested_deal_id == deal_id
        return CRMSystemActor("test")

    monkeypatch.setattr(
        "src.agents.tools.communication.get_email_details.require_agent_deal_access",
        _allow_deal,
    )

    result = await get_email_details(
        email.internet_message_id,
        context=context,
        include_thread=True,
    )

    assert result is not None
    assert result["access_scope"] == "deal"
    assert result["scope_id"] == str(deal_id)
    assert "deal_email_associations.deal_id" in session.statements[0]
    assert "deal_email_associations.status = 'ACTIVE'" in session.statements[0]
    assert "deal_email_associations.deal_id" in session.statements[1]
    assert "deal_email_associations.status = 'ACTIVE'" in session.statements[1]
    assert result["deal_associations"][0]["deal_id"] == str(deal_id)
    assert result["thread_message_count"] == 1


@pytest.mark.asyncio
async def test_get_email_details_rejects_invisible_deal(monkeypatch):
    """A deal identifier cannot bypass the same CRM visibility policy as deal tools."""
    organisation_id = uuid4()
    deal_id = uuid4()
    session = _FakeSession([])
    context = _FakeContext(organisation_id, session, user_id=uuid4(), deal_id=deal_id)

    async def _deny_deal(*_args, **_kwargs):
        raise AgentCRMAccessError("not visible")

    monkeypatch.setattr(
        "src.agents.tools.communication.get_email_details.require_agent_deal_access",
        _deny_deal,
    )

    result = await get_email_details("<hidden@example.com>", context=context)

    assert result is None
    assert session.statements == []


@pytest.mark.asyncio
async def test_get_email_details_caps_thread_messages_and_total_content(monkeypatch):
    """Large synchronized threads cannot exhaust the agent context in one tool call."""
    organisation_id = uuid4()
    integration_id = uuid4()
    latest = _make_email(
        organisation_id=organisation_id,
        internet_message_id="<latest@example.com>",
        received_at=datetime(2026, 7, 31, tzinfo=UTC),
        subject="Long thread",
        body_text="x" * 20_000,
        snippet="long",
    )
    latest.integration_id = integration_id
    history = []
    for day in range(26, 0, -1):
        message = _make_email(
            organisation_id=organisation_id,
            internet_message_id=f"<{day}@example.com>",
            received_at=datetime(2026, 7, day, tzinfo=UTC),
            subject="Long thread",
            body_text="x" * 20_000,
            snippet="long",
        )
        message.integration_id = integration_id
        history.append(message)
    session = _FakeSession([[_mailbox(integration_id)], [latest], history, []])
    context = _FakeContext(organisation_id, session, user_id=uuid4())

    async def _fake_actor(_context):
        return CRMSystemActor("test")

    monkeypatch.setattr(
        "src.agents.tools.communication.get_email_details.resolve_agent_access_actor",
        _fake_actor,
    )

    result = await get_email_details(
        latest.internet_message_id,
        context=context,
        mailbox_email="recipient@example.com",
        include_thread=True,
        max_body_length=100_000,
        max_thread_messages=100,
    )

    assert result is not None
    assert len(result["body_text"]) == 8_000
    assert result["thread_message_count"] == 7
    assert result["thread_content_characters"] == 52_000
    assert result["total_content_characters"] == 60_000
    assert result["thread_content_truncated"] is True
    assert result["thread_truncated"] is True
