"""Unit tests for internal_user_guard module."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from src.service.crm.guards.internal_user_guard import (
    _KEY_DOMAINS,
    _KEY_EMAILS,
    _KEY_LOADED,
    FREE_EMAIL_DOMAINS,
    _load_internal_user_data,
    is_internal_email,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value if isinstance(self._value, list) else []


class _FakeSession:
    """Session that returns canned responses for known query patterns."""

    def __init__(
        self,
        org_domains: list[str] | None = None,
        user_participant_emails: list[str] | None = None,
    ):
        self.org_domains = org_domains
        self.user_participant_emails = user_participant_emails or []
        self.statements: list[object] = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        compiled = str(stmt)
        # Detect which query is being run by inspecting compiled SQL text
        if "organisations" in compiled.lower() or "domain" in compiled.lower():
            return _FakeScalarResult(self.org_domains)
        if "participant_type" in compiled.lower():
            return _FakeScalarResult([(e,) for e in self.user_participant_emails])
        return _FakeScalarResult(None)

    async def commit(self):
        pass

    async def rollback(self):
        pass


@dataclass
class _FakeContext:
    organisation_id: UUID
    deal_id: UUID | None = None
    metadata: dict = field(default_factory=dict)
    _session: _FakeSession = field(default_factory=_FakeSession)

    @asynccontextmanager
    async def acquire_session(self):
        yield self._session


@pytest.mark.asyncio
async def test_participant_fallback_keeps_organisation_in_query_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deal ID alone must not define the participant tenant boundary."""
    organisation_id = uuid4()
    deal_id = uuid4()
    master_session = _FakeSession(org_domains=None)
    org_session = _FakeSession(user_participant_emails=[])
    context = _FakeContext(
        organisation_id=organisation_id,
        deal_id=deal_id,
        _session=org_session,
    )

    async def acquire_master_session():
        """Yield the tenant-free master-session fake used by the org-domain lookup."""
        yield master_session

    monkeypatch.setattr(
        "src.service.crm.guards.internal_user_guard.acquire_master_async_session",
        acquire_master_session,
    )

    await _load_internal_user_data(context)  # type: ignore[arg-type]

    participant_statement = org_session.statements[0]
    compiled = participant_statement.compile()
    assert organisation_id in compiled.params.values()
    assert deal_id in compiled.params.values()


# ---------------------------------------------------------------------------
# Tests: is_internal_email
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_internal_email_domain_match() -> None:
    """Email whose domain matches org domains is internal."""
    ctx = _FakeContext(
        organisation_id=uuid4(),
        deal_id=uuid4(),
        metadata={
            _KEY_LOADED: True,
            _KEY_DOMAINS: {"acme.com", "acme.io"},
            _KEY_EMAILS: set(),
        },
    )
    assert await is_internal_email(ctx, "seller@acme.com") is True  # type: ignore[arg-type]
    assert await is_internal_email(ctx, "TEAM@Acme.IO") is True  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_is_internal_email_exact_match() -> None:
    """Email that matches an internal email exactly is internal."""
    ctx = _FakeContext(
        organisation_id=uuid4(),
        deal_id=uuid4(),
        metadata={
            _KEY_LOADED: True,
            _KEY_DOMAINS: set(),
            _KEY_EMAILS: {"owner@personal.com"},
        },
    )
    assert await is_internal_email(ctx, "owner@personal.com") is True  # type: ignore[arg-type]
    assert await is_internal_email(ctx, "Owner@Personal.com") is True  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_is_internal_email_external_passes() -> None:
    """External emails are not flagged as internal."""
    ctx = _FakeContext(
        organisation_id=uuid4(),
        deal_id=uuid4(),
        metadata={
            _KEY_LOADED: True,
            _KEY_DOMAINS: {"acme.com"},
            _KEY_EMAILS: {"owner@acme.com"},
        },
    )
    assert await is_internal_email(ctx, "buyer@customer.com") is False  # type: ignore[arg-type]
    assert await is_internal_email(ctx, "prospect@external.org") is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_cache_reused_across_calls() -> None:
    """Once loaded, subsequent calls reuse the cache (no extra DB hit)."""
    ctx = _FakeContext(
        organisation_id=uuid4(),
        deal_id=uuid4(),
        metadata={
            _KEY_LOADED: True,
            _KEY_DOMAINS: {"acme.com"},
            _KEY_EMAILS: set(),
        },
    )
    # Call twice — both should work from cache
    result1 = await is_internal_email(ctx, "x@acme.com")  # type: ignore[arg-type]
    result2 = await is_internal_email(ctx, "y@acme.com")  # type: ignore[arg-type]
    assert result1 is True
    assert result2 is True
    assert ctx.metadata[_KEY_LOADED] is True


@pytest.mark.asyncio
async def test_no_deal_id_returns_empty_guard() -> None:
    """When deal_id is None and org domains are empty, nothing is blocked."""
    session = _FakeSession(
        org_domains=None,
        user_participant_emails=["rep@acme.com"],
    )

    ctx = _FakeContext(
        organisation_id=uuid4(),
        deal_id=None,  # No deal — participant fallback skipped
        _session=session,
    )

    # No org domains, no deal to derive from — everything passes
    assert await is_internal_email(ctx, "rep@acme.com") is False  # type: ignore[arg-type]
    assert await is_internal_email(ctx, "anyone@acme.com") is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_participant_fallback_when_org_domains_empty() -> None:
    """When Organisation.domain is empty, domains are derived from USER participants."""
    org_id = uuid4()
    deal_id = uuid4()

    session = _FakeSession(
        org_domains=None,  # No org domains configured
        user_participant_emails=["seller@acme.com", "rep@acme.com"],
    )

    ctx = _FakeContext(
        organisation_id=org_id,
        deal_id=deal_id,
        _session=session,
    )

    # acme.com derived from participant emails
    assert await is_internal_email(ctx, "anyone@acme.com") is True  # type: ignore[arg-type]
    # Exact participant email also matched
    assert await is_internal_email(ctx, "seller@acme.com") is True  # type: ignore[arg-type]
    # External email passes
    assert await is_internal_email(ctx, "buyer@customer.com") is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_participant_fallback_filters_free_email_domains() -> None:
    """Free email providers (gmail, hotmail) are excluded from derived domains
    but the exact participant email is still blocked."""
    org_id = uuid4()
    deal_id = uuid4()

    session = _FakeSession(
        org_domains=None,  # No org domains configured
        user_participant_emails=["john@gmail.com", "rep@acme.com"],
    )

    ctx = _FakeContext(
        organisation_id=org_id,
        deal_id=deal_id,
        _session=session,
    )

    # gmail.com NOT added to domain set — random gmail users are allowed
    assert await is_internal_email(ctx, "buyer@gmail.com") is False  # type: ignore[arg-type]
    # But the exact participant email IS blocked
    assert await is_internal_email(ctx, "john@gmail.com") is True  # type: ignore[arg-type]
    # acme.com IS in domain set (not a free provider)
    assert await is_internal_email(ctx, "anyone@acme.com") is True  # type: ignore[arg-type]
    # Sanity: free email domains constant has expected providers
    assert "gmail.com" in FREE_EMAIL_DOMAINS
    assert "hotmail.com" in FREE_EMAIL_DOMAINS


@pytest.mark.asyncio
async def test_org_domains_skip_participant_fallback() -> None:
    """When Organisation.domain has values, participant fallback is NOT used."""
    org_id = uuid4()
    deal_id = uuid4()

    session = _FakeSession(
        org_domains=["acme.com"],
        # These participants should be ignored since org domains exist
        user_participant_emails=["rep@otherdomain.com"],
    )

    ctx = _FakeContext(
        organisation_id=org_id,
        deal_id=deal_id,
        _session=session,
    )

    # acme.com from org domains works
    assert await is_internal_email(ctx, "anyone@acme.com") is True  # type: ignore[arg-type]
    # otherdomain.com from participants NOT loaded (org domains already present)
    assert await is_internal_email(ctx, "someone@otherdomain.com") is False  # type: ignore[arg-type]
    # Exact participant email also NOT in the set
    assert await is_internal_email(ctx, "rep@otherdomain.com") is False  # type: ignore[arg-type]
