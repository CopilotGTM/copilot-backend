"""Contract tests for single-message and full-thread email detail reads."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import MultipleResultsFound
from src.agents.tools.communication.get_email_details import (
    _format_email_details,
    _hydrate_attachment_manifests,
    get_email_details,
)
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


def _make_email(
    *,
    organisation_id,
    internet_message_id,
    received_at,
    subject,
    body_text,
    snippet,
    attachments=None,
):
    attachment_items = list(attachments or [])
    return SimpleNamespace(
        email_id=uuid4(),
        integration_id=uuid4(),
        organisation_id=organisation_id,
        external_message_id=f"provider-{internet_message_id}",
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
        has_attachments=bool(attachment_items),
        labels=["inbox"],
        attachments=attachment_items,
        body_text=body_text,
        body_html=None,
        snippet=snippet,
    )


def test_email_details_attachment_manifest_identifies_its_owning_message():
    """An attachment handle is scoped to one RFC message, never the whole thread."""
    email = _make_email(
        organisation_id=uuid4(),
        internet_message_id="<proposal@example.com>",
        received_at=datetime.now(UTC),
        subject="Proposal",
        body_text="Please review.",
        snippet="Please review.",
        attachments=[
            {
                "attachment_id": "provider-att-1",
                "name": "proposal.pdf",
                "size": 42_000,
                "mime_type": "application/pdf",
            }
        ],
    )

    result = _format_email_details(
        email,
        include_body=False,
        include_html=False,
        max_body_length=8_000,
    )

    assert result["attachments"] == [
        {
            "attachment_id": "provider-att-1",
            "internet_message_id": "<proposal@example.com>",
            "filename": "proposal.pdf",
            "size_bytes": 42_000,
            "mime_type": "application/pdf",
            "readable": True,
        }
    ]


def test_email_details_marks_attachment_without_provider_id_unreadable():
    """Legacy metadata remains visible but cannot authorize a provider download."""
    email = _make_email(
        organisation_id=uuid4(),
        internet_message_id="<legacy@example.com>",
        received_at=datetime.now(UTC),
        subject="Legacy",
        body_text="Old metadata",
        snippet="Old metadata",
        attachments=[
            {
                "filename": "legacy.pdf",
                "size_bytes": 10,
                "mime_type": "application/pdf",
            }
        ],
    )

    result = _format_email_details(
        email,
        include_body=False,
        include_html=False,
        max_body_length=8_000,
    )

    assert result["attachments"][0]["readable"] is False
    assert result["attachments"][0]["attachment_id"] is None


def test_email_details_preserves_cached_unreadable_attachment_reason():
    """Caching provider metadata must not make an unsupported file appear readable."""
    email = _make_email(
        organisation_id=uuid4(),
        internet_message_id="<archive@example.com>",
        received_at=datetime.now(UTC),
        subject="Archive",
        body_text="Attached.",
        snippet="Attached.",
        attachments=[
            {
                "attachment_id": "att-zip",
                "filename": "archive.zip",
                "size_bytes": 5_000,
                "mime_type": "application/zip",
                "readable": False,
                "unreadable_reason": "unsupported_attachment_type",
            }
        ],
    )

    result = _format_email_details(
        email,
        include_body=False,
        include_html=False,
        max_body_length=8_000,
    )

    assert result["attachments"][0]["readable"] is False
    assert result["attachments"][0]["unreadable_reason"] == "unsupported_attachment_type"


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
        credentials={"access_token": "test-token"},
        access_token_expires_at=None,
        connection_metadata={},
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
async def test_get_email_details_hydrates_each_thread_message_manifest_once(monkeypatch):
    """Thread attachment counts use message ownership and ignore the repeated top level."""
    organisation_id = uuid4()
    integration_id = uuid4()
    older = _make_email(
        organisation_id=organisation_id,
        internet_message_id="<older@example.com>",
        received_at=datetime(2026, 7, 13, tzinfo=UTC),
        subject="Re: Documents",
        body_text="Original",
        snippet="Original",
    )
    latest = _make_email(
        organisation_id=organisation_id,
        internet_message_id="<latest@example.com>",
        received_at=datetime(2026, 7, 14, tzinfo=UTC),
        subject="Re: Documents",
        body_text="Response",
        snippet="Response",
    )
    for message in (older, latest):
        message.integration_id = integration_id
        message.has_attachments = True

    session = _FakeSession([[_mailbox(integration_id)], [latest], [latest, older], []])
    context = _FakeContext(organisation_id, session, user_id=uuid4())
    listed_message_ids: list[str] = []

    async def _fake_actor(_context):
        return CRMSystemActor("test")

    async def _list_attachments(internet_message_id, **_kwargs):
        listed_message_ids.append(internet_message_id)
        count = 2 if internet_message_id == older.internet_message_id else 1
        return {
            "status": "success",
            "attachments": [
                {
                    "attachment_id": f"{internet_message_id}-att-{index}",
                    "internet_message_id": internet_message_id,
                    "filename": f"document-{index}.pdf",
                    "size_bytes": 10_000,
                    "mime_type": "application/pdf",
                    "readable": True,
                    "unreadable_reason": None,
                }
                for index in range(count)
            ],
        }

    monkeypatch.setattr(
        "src.agents.tools.communication.get_email_details.resolve_agent_access_actor",
        _fake_actor,
    )
    monkeypatch.setattr(
        "src.agents.tools.communication.get_email_details.list_email_attachments",
        _list_attachments,
    )

    result = await get_email_details(
        latest.internet_message_id,
        context=context,
        mailbox_email="recipient@example.com",
        include_thread=True,
    )

    assert result is not None
    assert listed_message_ids == [latest.internet_message_id, older.internet_message_id]
    assert len(result["attachments"]) == 1
    assert [len(item["attachments"]) for item in result["thread_messages"]] == [2, 1]
    assert result["thread_attachment_instance_count"] == 3
    assert result["thread_readable_attachment_count"] == 3


@pytest.mark.asyncio
async def test_attachment_manifest_hydration_has_bounded_parallelism(monkeypatch):
    """Thread hydration overlaps provider latency without opening every message at once."""
    message_ids = [f"<message-{index}@example.com>" for index in range(6)]
    output = {
        "internet_message_id": message_ids[0],
        "has_attachments": True,
        "attachments": [],
        "thread_messages": [
            {
                "internet_message_id": message_id,
                "has_attachments": True,
                "attachments": [],
            }
            for message_id in message_ids
        ],
    }
    active = 0
    peak = 0
    first_wave_started = asyncio.Event()
    release = asyncio.Event()

    async def _list_attachments(internet_message_id, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 4:
            first_wave_started.set()
        await release.wait()
        active -= 1
        return {
            "status": "success",
            "attachments": [
                {
                    "attachment_id": f"{internet_message_id}-att",
                    "internet_message_id": internet_message_id,
                    "filename": "document.pdf",
                    "size_bytes": 10_000,
                    "mime_type": "application/pdf",
                    "readable": True,
                }
            ],
        }

    monkeypatch.setattr(
        "src.agents.tools.communication.get_email_details.list_email_attachments",
        _list_attachments,
    )

    hydration = asyncio.create_task(
        _hydrate_attachment_manifests(
            output,
            context=SimpleNamespace(),
            mailbox_email="seller@example.com",
            deal_id=None,
        )
    )
    try:
        await asyncio.wait_for(first_wave_started.wait(), timeout=0.5)
    finally:
        release.set()
        await hydration

    assert peak == 4


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
        "src.agents.tools.communication.email_attachments.require_agent_deal_access",
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
async def test_get_email_details_keeps_synced_deal_email_readable_without_provider(
    monkeypatch,
):
    """A disconnected provider must not hide an already-authorized local email body."""
    organisation_id = uuid4()
    deal_id = uuid4()
    email = _make_email(
        organisation_id=organisation_id,
        internet_message_id="<stored@example.com>",
        received_at=datetime.now(UTC),
        subject="Stored email",
        body_text="This body is already synchronized.",
        snippet="This body is already synchronized.",
    )
    association = _make_association(deal_id)
    session = _FakeSession([[email], [email], [association]])
    context = _FakeContext(organisation_id, session, user_id=uuid4(), deal_id=deal_id)

    async def _allow_deal(*_args, **_kwargs):
        return CRMSystemActor("test")

    monkeypatch.setattr(
        "src.agents.tools.communication.email_attachments.require_agent_deal_access",
        _allow_deal,
    )

    result = await get_email_details(
        email.internet_message_id,
        context=context,
        include_thread=True,
    )

    assert result is not None
    assert result["body_text"] == "This body is already synchronized."


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
        "src.agents.tools.communication.email_attachments.require_agent_deal_access",
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
