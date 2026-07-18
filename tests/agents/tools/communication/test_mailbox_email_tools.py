"""Contract tests for mailbox account, message, and thread search tools."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from src.agents.tools.communication.mailbox_emails import (
    _enrich_thread_summaries,
    create_list_connected_email_accounts,
    create_search_mailbox_emails,
    list_connected_email_accounts,
    search_mailbox_emails,
)
from src.agents.tools.registry import ToolName, ToolRegistry
from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.google_integration_scopes import GOOGLE_SCOPE_GMAIL_READONLY
from src.service.crm.access.record_access_policy import CRMSystemActor


class _FakeResult:
    def __init__(self, items):
        self._items = list(items)

    def scalars(self):
        """Mirror the SQLAlchemy scalar-result chain used by the tool."""
        return self

    def all(self):
        """Return the configured rows without database behavior."""
        return list(self._items)


class _FakeSession:
    def __init__(self, execute_results):
        self._execute_results = list(execute_results)
        self.statements = []

    async def execute(self, stmt):
        """Record compiled SQL and return the next deterministic result."""
        self.statements.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
        return _FakeResult(self._execute_results.pop(0))


class _FakeContext:
    def __init__(self, *, organisation_id, user_id, session):
        self.organisation_id = organisation_id
        self.user_id = user_id
        self._session = session

    @asynccontextmanager
    async def acquire_session(self):
        """Yield the fake org-scoped session expected by agent tools."""
        yield self._session


def _integration(**overrides):
    data = {
        "integration_id": uuid4(),
        "organisation_id": UUID("11111111-1111-1111-1111-111111111111"),
        "created_by_user_id": UUID("22222222-2222-2222-2222-222222222222"),
        "integration_type": IntegrationType.GOOGLE,
        "integration_email": "leads@example.com",
        "name": "Leads Gmail",
        "is_active": True,
        "requires_reauth": False,
        "scopes": [GOOGLE_SCOPE_GMAIL_READONLY],
        "last_synced_at": datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _microsoft_integration(**overrides):
    data = {
        "integration_type": IntegrationType.MICROSOFT,
        "integration_email": "sales@copilotgtm.com",
        "name": "Microsoft",
        "scopes": ["User.Read", "Mail.Read"],
    }
    data.update(overrides)
    return _integration(**data)


def _email(**overrides):
    data = {
        "email_id": uuid4(),
        "integration_id": UUID("33333333-3333-3333-3333-333333333333"),
        "internet_message_id": "<lead@example.com>",
        "thread_id": "thread-1",
        "subject": "New inbound lead",
        "from_email": "buyer@acme.com",
        "from_name": "Buyer",
        "to_emails": ["leads@example.com"],
        "cc_emails": [],
        "received_at": datetime.now(UTC) - timedelta(days=2),
        "sent_at": None,
        "is_read": False,
        "is_starred": False,
        "is_important": True,
        "has_attachments": False,
        "labels": ["INBOX"],
        "snippet": "I want to evaluate CopilotGTM",
        "body_text": "I want to evaluate CopilotGTM for Acme.",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.mark.asyncio
async def test_list_connected_email_accounts_returns_readable_gmail_accounts() -> None:
    """Read readiness must reflect provider scope and reauthorization state."""
    organisation_id = UUID("11111111-1111-1111-1111-111111111111")
    user_id = UUID("22222222-2222-2222-2222-222222222222")
    readable = _integration()
    reauth = _integration(
        integration_email="old@example.com",
        requires_reauth=True,
        scopes=[],
    )
    outlook = _microsoft_integration()
    outlook_missing_read = _microsoft_integration(
        integration_email="calendar-only@copilotgtm.com",
        scopes=["User.Read", "Calendars.ReadWrite"],
    )
    session = _FakeSession([[readable, reauth, outlook, outlook_missing_read]])
    context = _FakeContext(organisation_id=organisation_id, user_id=user_id, session=session)

    result = await list_connected_email_accounts(context=context)

    assert result == [
        {
            "integration_id": str(readable.integration_id),
            "integration_email": "leads@example.com",
            "provider": "google",
            "name": "Leads Gmail",
            "is_active": True,
            "requires_reauth": False,
            "can_read_email": True,
            "missing_read_scopes": [],
            "last_synced_at": "2026-07-01T08:00:00+00:00",
        },
        {
            "integration_id": str(reauth.integration_id),
            "integration_email": "old@example.com",
            "provider": "google",
            "name": "Leads Gmail",
            "is_active": True,
            "requires_reauth": True,
            "can_read_email": False,
            "missing_read_scopes": ["Gmail access"],
            "last_synced_at": "2026-07-01T08:00:00+00:00",
        },
        {
            "integration_id": str(outlook.integration_id),
            "integration_email": "sales@copilotgtm.com",
            "provider": "microsoft",
            "name": "Microsoft",
            "is_active": True,
            "requires_reauth": False,
            "can_read_email": True,
            "missing_read_scopes": [],
            "last_synced_at": "2026-07-01T08:00:00+00:00",
        },
        {
            "integration_id": str(outlook_missing_read.integration_id),
            "integration_email": "calendar-only@copilotgtm.com",
            "provider": "microsoft",
            "name": "Microsoft",
            "is_active": True,
            "requires_reauth": False,
            "can_read_email": False,
            "missing_read_scopes": ["Outlook mail access"],
            "last_synced_at": "2026-07-01T08:00:00+00:00",
        },
    ]
    assert (
        "knowledge_integrations.integration_type IN ('google', 'microsoft')"
        in session.statements[0]
    )


@pytest.mark.asyncio
async def test_search_mailbox_emails_requires_exact_connected_mailbox() -> None:
    """A user cannot broaden a mailbox read beyond an exact owned integration."""
    organisation_id = UUID("11111111-1111-1111-1111-111111111111")
    user_id = UUID("22222222-2222-2222-2222-222222222222")
    session = _FakeSession([[]])
    context = _FakeContext(organisation_id=organisation_id, user_id=user_id, session=session)

    result = await search_mailbox_emails(
        context=context,
        mailbox_email="missing@example.com",
        days_back=14,
    )

    assert result == {
        "status": "error",
        "code": "mailbox_not_found",
        "message": "I could not find a connected readable mailbox for missing@example.com.",
    }


@pytest.mark.asyncio
async def test_search_mailbox_emails_filters_by_resolved_integration_and_days_back() -> None:
    """Message search must retain its original integration and date boundaries."""
    organisation_id = UUID("11111111-1111-1111-1111-111111111111")
    user_id = UUID("22222222-2222-2222-2222-222222222222")
    integration_id = UUID("33333333-3333-3333-3333-333333333333")
    mailbox = _integration(integration_id=integration_id)
    email = _email(integration_id=integration_id)
    session = _FakeSession([[mailbox], [email]])
    context = _FakeContext(organisation_id=organisation_id, user_id=user_id, session=session)

    result = await search_mailbox_emails(
        context=context,
        mailbox_email=" Leads@Example.com ",
        days_back=14,
        query="evaluate",
        include_body_preview=True,
    )

    assert result["status"] == "success"
    assert result["mailbox_email"] == "leads@example.com"
    assert result["items"] == [
        {
            "email_id": str(email.email_id),
            "integration_id": str(integration_id),
            "internet_message_id": "<lead@example.com>",
            "thread_id": "thread-1",
            "subject": "New inbound lead",
            "from_email": "buyer@acme.com",
            "from_name": "Buyer",
            "to_emails": ["leads@example.com"],
            "cc_emails": [],
            "received_at": email.received_at.isoformat(),
            "sent_at": None,
            "is_read": False,
            "is_starred": False,
            "is_important": True,
            "has_attachments": False,
            "labels": ["INBOX"],
            "snippet": "I want to evaluate CopilotGTM",
            "body_preview": "I want to evaluate CopilotGTM for Acme.",
        }
    ]
    assert "emails.integration_id = '33333333333333333333333333333333'" in session.statements[1]
    assert "emails.received_at >=" in session.statements[1]


@pytest.mark.asyncio
async def test_thread_search_uses_full_history_to_compute_pending_reply(monkeypatch) -> None:
    """A date window discovers threads; it must not discard their older replies."""
    organisation_id = UUID("11111111-1111-1111-1111-111111111111")
    user_id = UUID("22222222-2222-2222-2222-222222222222")
    integration_id = UUID("33333333-3333-3333-3333-333333333333")
    mailbox = _integration(integration_id=integration_id)
    older_outbound = _email(
        integration_id=integration_id,
        internet_message_id="<outbound@example.com>",
        from_email="leads@example.com",
        to_emails=["buyer@acme.com"],
        received_at=datetime(2026, 7, 13, 8, 0, tzinfo=UTC),
        is_read=True,
    )
    recent_inbound = _email(
        integration_id=integration_id,
        internet_message_id="<inbound@example.com>",
        received_at=datetime(2026, 7, 14, 8, 0, tzinfo=UTC),
    )
    session = _FakeSession([[mailbox], [recent_inbound], [older_outbound, recent_inbound]])
    context = _FakeContext(organisation_id=organisation_id, user_id=user_id, session=session)

    async def _fake_domains(_organisation_id):
        return {"example.com"}

    async def _fake_enrichment(*, context, summaries):
        for summary in summaries:
            summary["deals"] = []
            summary["leads"] = []
        return True

    monkeypatch.setattr(
        "src.service.crm.utils.internal_domains.get_internal_domains", _fake_domains
    )
    monkeypatch.setattr(
        "src.agents.tools.communication.mailbox_emails._enrich_thread_summaries",
        _fake_enrichment,
    )

    result = await search_mailbox_emails(
        context=context,
        mailbox_email="leads@example.com",
        result_mode="threads",
        attention_filter="pending_from_me",
        start_at="2026-07-14T00:00:00+00:00",
        end_at="2026-07-15T00:00:00+00:00",
    )

    assert result["status"] == "success"
    assert result["result_mode"] == "threads"
    assert result["items"][0]["message_count"] == 2
    assert result["items"][0]["attention_state"] == "pending_from_me"
    assert result["items"][0]["external_participants"] == ["buyer@acme.com"]
    assert "emails.received_at < '2026-07-15 00:00:00+00:00'" in session.statements[1]
    assert "emails.received_at >= '2026-07-14 00:00:00+00:00'" in session.statements[1]
    assert "emails.received_at >=" not in session.statements[2]
    assert "row_number() OVER" in session.statements[1]
    assert "ranked_mailbox_threads.thread_rank = 1" in session.statements[1]
    assert "ranked_thread_history.history_rank <= 26" in session.statements[2]


@pytest.mark.asyncio
async def test_thread_search_paginates_distinct_candidates_with_progress(monkeypatch) -> None:
    """A full candidate batch must return a usable cursor and no repeated threads."""
    organisation_id = UUID("11111111-1111-1111-1111-111111111111")
    user_id = UUID("22222222-2222-2222-2222-222222222222")
    integration_id = UUID("33333333-3333-3333-3333-333333333333")
    mailbox = _integration(integration_id=integration_id)
    candidates = [
        _email(
            integration_id=integration_id,
            email_id=UUID(int=1000 - index),
            internet_message_id=f"<message-{index}@example.com>",
            thread_id=f"thread-{index}",
            received_at=datetime.now(UTC) - timedelta(hours=index),
        )
        for index in range(101)
    ]

    async def _fake_domains(_organisation_id):
        return {"example.com"}

    async def _fake_enrichment(*, context, summaries):
        for summary in summaries:
            summary["deals"] = []
            summary["leads"] = []
        return True

    monkeypatch.setattr(
        "src.service.crm.utils.internal_domains.get_internal_domains", _fake_domains
    )
    monkeypatch.setattr(
        "src.agents.tools.communication.mailbox_emails._enrich_thread_summaries",
        _fake_enrichment,
    )

    first_session = _FakeSession([[mailbox], candidates, candidates[:100]])
    first_context = _FakeContext(
        organisation_id=organisation_id,
        user_id=user_id,
        session=first_session,
    )
    first_page = await search_mailbox_emails(
        context=first_context,
        mailbox_email="leads@example.com",
        result_mode="threads",
        limit=100,
    )

    assert len(first_page["items"]) == 20
    assert first_page["has_more"] is True
    assert first_page["next_before_received_at"] is not None
    assert first_page["next_before_email_id"] is not None
    assert "LIMIT 101" in first_session.statements[1]

    remaining = candidates[20:]
    second_session = _FakeSession([[mailbox], remaining, remaining])
    second_context = _FakeContext(
        organisation_id=organisation_id,
        user_id=user_id,
        session=second_session,
    )
    second_page = await search_mailbox_emails(
        context=second_context,
        mailbox_email="leads@example.com",
        result_mode="threads",
        limit=100,
        before_received_at=first_page["next_before_received_at"],
        before_email_id=first_page["next_before_email_id"],
    )

    first_ids = {item["latest_internet_message_id"] for item in first_page["items"]}
    second_ids = {item["latest_internet_message_id"] for item in second_page["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert "ranked_mailbox_threads.received_at <" in second_session.statements[1]


@pytest.mark.asyncio
async def test_thread_enrichment_adds_visible_deal_and_predeal_lead(monkeypatch) -> None:
    """CRM evidence may enrich a mailbox thread but never creates the candidate."""
    organisation_id = UUID("11111111-1111-1111-1111-111111111111")
    association = SimpleNamespace(
        internet_message_id="<inbound@example.com>",
    )
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Acme expansion",
        company_name="Acme",
        stage_label="Negotiation",
        stage_name="Negotiation",
        deal_status="open",
        priority="strategic",
        amount=100_000,
        currency="USD",
        expected_close_date=datetime(2026, 7, 31, tzinfo=UTC),
    )
    lead = SimpleNamespace(
        lead_id=uuid4(),
        full_name="Buyer",
        company_name="Acme",
        email="buyer@acme.com",
        company_domain="acme.com",
        status="working",
        priority="high",
        score=95,
    )
    context = _FakeContext(
        organisation_id=organisation_id,
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        session=_FakeSession([[(association, deal)], [lead]]),
    )
    summaries = [
        {
            "_internet_message_ids": ["<inbound@example.com>"],
            "external_participants": ["buyer@acme.com"],
        }
    ]

    async def _fake_actor(_context):
        return CRMSystemActor("test")

    monkeypatch.setattr(
        "src.agents.tools.communication.mailbox_emails.resolve_agent_access_actor",
        _fake_actor,
    )

    available = await _enrich_thread_summaries(context=context, summaries=summaries)

    assert available is True
    assert summaries[0]["deals"][0]["stage"] == "Negotiation"
    assert summaries[0]["deals"][0]["priority"] == "strategic"
    assert summaries[0]["leads"][0]["priority"] == "high"


@pytest.mark.asyncio
async def test_search_mailbox_emails_accepts_readable_outlook_mailbox() -> None:
    """The same mailbox contract must work for readable Microsoft accounts."""
    organisation_id = UUID("11111111-1111-1111-1111-111111111111")
    user_id = UUID("22222222-2222-2222-2222-222222222222")
    integration_id = UUID("44444444-4444-4444-4444-444444444444")
    mailbox = _microsoft_integration(integration_id=integration_id)
    email = _email(
        integration_id=integration_id,
        to_emails=["sales@copilotgtm.com"],
        labels=["Inbox"],
    )
    session = _FakeSession([[mailbox], [email]])
    context = _FakeContext(organisation_id=organisation_id, user_id=user_id, session=session)

    result = await search_mailbox_emails(
        context=context,
        mailbox_email="sales@copilotgtm.com",
        days_back=14,
    )

    assert result["status"] == "success"
    assert result["mailbox_email"] == "sales@copilotgtm.com"
    assert result["integration_id"] == str(integration_id)
    assert len(result["items"]) == 1
    assert (
        "knowledge_integrations.integration_type IN ('google', 'microsoft')"
        in session.statements[0]
    )
    assert "emails.integration_id = '44444444444444444444444444444444'" in session.statements[1]


@pytest.mark.asyncio
async def test_search_mailbox_emails_returns_cursor_for_next_page() -> None:
    """Message pagination must expose the last returned stable ordering key."""
    organisation_id = UUID("11111111-1111-1111-1111-111111111111")
    user_id = UUID("22222222-2222-2222-2222-222222222222")
    integration_id = UUID("33333333-3333-3333-3333-333333333333")
    mailbox = _integration(integration_id=integration_id)
    first = _email(
        integration_id=integration_id,
        email_id=UUID("99999999-9999-9999-9999-999999999999"),
        received_at=datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
    )
    second = _email(
        integration_id=integration_id,
        email_id=UUID("88888888-8888-8888-8888-888888888888"),
        received_at=datetime(2026, 7, 1, 7, 0, tzinfo=UTC),
    )
    third = _email(
        integration_id=integration_id,
        email_id=UUID("77777777-7777-7777-7777-777777777777"),
        received_at=datetime(2026, 7, 1, 6, 0, tzinfo=UTC),
    )
    session = _FakeSession([[mailbox], [first, second, third]])
    context = _FakeContext(organisation_id=organisation_id, user_id=user_id, session=session)

    result = await search_mailbox_emails(
        context=context,
        mailbox_email="leads@example.com",
        limit=2,
        before_received_at="2026-07-01T09:00:00+00:00",
    )

    assert result["status"] == "success"
    assert len(result["items"]) == 2
    assert result["has_more"] is True
    assert result["next_before_received_at"] == "2026-07-01T07:00:00+00:00"
    assert result["next_before_email_id"] == "88888888-8888-8888-8888-888888888888"
    assert "emails.received_at < '2026-07-01 09:00:00+00:00'" in session.statements[1]
    assert "LIMIT 3" in session.statements[1]


def test_general_registry_exposes_mailbox_email_tools() -> None:
    """General agents must receive both mailbox discovery and search tools."""
    assert ToolName.LIST_CONNECTED_EMAIL_ACCOUNTS in ToolRegistry.get_available_tools_for_agent(
        "GeneralAgent"
    )
    assert ToolName.SEARCH_MAILBOX_EMAILS in ToolRegistry.get_available_tools_for_agent(
        "GeneralAgent"
    )


def test_mailbox_tool_factories_are_exported() -> None:
    """Public tool factories must remain importable for registry wiring."""
    assert callable(create_list_connected_email_accounts)
    assert callable(create_search_mailbox_emails)
