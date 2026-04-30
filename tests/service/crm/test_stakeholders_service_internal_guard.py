"""Tests for internal email guard in StakeholdersService.create_stakeholder."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    DealStakeholder,
    StakeholderSource,
)
from src.service.crm.insights.stakeholders_service import (
    StakeholdersService,
    StakeholdersServiceError,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeScalarResult:
    """Tiny scalar-result stand-in used by fake session execute calls."""

    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        """Return the scalar value that would come from SQLAlchemy scalar_one_or_none."""
        return self._value

    def scalars(self):
        """Return self so caller can chain `.all()` in tests."""
        return self

    def all(self):
        """Return a list-like collection when caller requests scalars().all()."""
        return self._value if isinstance(self._value, list) else []


class _FakeSession:
    """Minimal session that returns canned Organisation.domain values."""

    def __init__(self, org_domains: list[str] | None = None):
        self.org_domains = org_domains
        self.executed_statements = []
        self.added = []
        self.committed = False
        self.rolled_back = False
        self.existing_stakeholder = None

    async def execute(self, stmt):
        """Capture executed SQL-like statements and return canned scalar results."""
        compiled = str(stmt)
        if "organisations" in compiled.lower() or "domain" in compiled.lower():
            return _FakeScalarResult(self.org_domains)
        # For the duplicate-check query, return None (no existing stakeholder)
        return _FakeScalarResult(None)

    async def commit(self):
        """Capture commit intent for assertions."""
        self.committed = True

    async def rollback(self):
        """Capture rollback intent for assertions."""
        self.rolled_back = True

    def add(self, obj):
        """Track rows that would be inserted/updated."""
        self.added.append(obj)

    async def refresh(self, obj):
        """No-op refresh hook for test sessions."""
        return None


# ---------------------------------------------------------------------------
# Tests: _is_internal_email
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_internal_email_matches_org_domain() -> None:
    """Email whose domain matches Organisation.domain is internal."""
    session = _FakeSession(org_domains=["acme.com", "acme.io"])
    service = StakeholdersService(org_session=session, organisation_id=uuid4())  # type: ignore[arg-type]
    service._get_internal_domains = AsyncMock(return_value={"acme.com", "acme.io"})  # type: ignore[method-assign]

    assert await service._is_internal_email("seller@acme.com") is True
    assert await service._is_internal_email("TEAM@Acme.IO") is True


@pytest.mark.asyncio
async def test_is_internal_email_allows_external() -> None:
    """External emails are not flagged as internal."""
    session = _FakeSession(org_domains=["acme.com"])
    service = StakeholdersService(org_session=session, organisation_id=uuid4())  # type: ignore[arg-type]
    service._get_internal_domains = AsyncMock(return_value={"acme.com"})  # type: ignore[method-assign]

    assert await service._is_internal_email("buyer@customer.com") is False
    assert await service._is_internal_email("prospect@external.org") is False


@pytest.mark.asyncio
async def test_is_internal_email_no_org_domains() -> None:
    """When no org domains configured, nothing is blocked."""
    session = _FakeSession(org_domains=None)
    service = StakeholdersService(org_session=session, organisation_id=uuid4())  # type: ignore[arg-type]
    service._get_internal_domains = AsyncMock(return_value=set())  # type: ignore[method-assign]

    assert await service._is_internal_email("anyone@acme.com") is False


@pytest.mark.asyncio
async def test_is_internal_email_empty_or_no_at() -> None:
    """Invalid email formats return False (not internal)."""
    session = _FakeSession(org_domains=["acme.com"])
    service = StakeholdersService(org_session=session, organisation_id=uuid4())  # type: ignore[arg-type]

    assert await service._is_internal_email("") is False
    assert await service._is_internal_email("no-at-sign") is False


# ---------------------------------------------------------------------------
# Tests: create_stakeholder rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_stakeholder_rejects_internal_email() -> None:
    """create_stakeholder raises 403 for internal team member emails."""
    from src.api.schemas.crm_models import CreateStakeholderRequest
    from src.domain.models.organisation_tables.sync.crm.stakeholders import (
        DecisionPower,
        StakeholderStance,
    )

    session = _FakeSession(org_domains=["mycompany.com"])
    service = StakeholdersService(org_session=session, organisation_id=uuid4())  # type: ignore[arg-type]
    service._is_internal_email = AsyncMock(return_value=True)  # type: ignore[method-assign]

    request = CreateStakeholderRequest(
        email="seller@mycompany.com",
        name="Internal Seller",
        stance=StakeholderStance.UNKNOWN,
        decision_power=DecisionPower.UNKNOWN,
    )

    with pytest.raises(StakeholdersServiceError) as exc_info:
        await service.create_stakeholder(deal_id=uuid4(), request=request)

    assert exc_info.value.status_code == 403
    assert "internal team member" in exc_info.value.message.lower()


@pytest.mark.asyncio
async def test_create_stakeholder_allows_external_email() -> None:
    """create_stakeholder does NOT reject external emails."""
    from src.api.schemas.crm_models import CreateStakeholderRequest
    from src.domain.models.organisation_tables.sync.crm.stakeholders import (
        DecisionPower,
        StakeholderStance,
    )

    session = _FakeSession(org_domains=["mycompany.com"])
    service = StakeholdersService(org_session=session, organisation_id=uuid4())  # type: ignore[arg-type]
    service._is_internal_email = AsyncMock(return_value=False)  # type: ignore[method-assign]

    request = CreateStakeholderRequest(
        email="buyer@customer.com",
        name="External Buyer",
        stance=StakeholderStance.UNKNOWN,
        decision_power=DecisionPower.UNKNOWN,
    )

    # This session is intentionally fake, so DB writes cannot complete in this
    # test while still validating that internal-email filtering is skipped.
    try:
        await service.create_stakeholder(deal_id=uuid4(), request=request)
    except StakeholdersServiceError as e:
        # 409 (duplicate) or other errors are fine — just NOT 403
        assert e.status_code != 403, f"External email should not be rejected: {e.message}"
    except Exception:  # noqa: S110
        # Any non-StakeholdersServiceError is OK (fake session limitations)
        pass


@pytest.mark.asyncio
async def test_create_stakeholder_reactivates_removed_existing_stakeholder() -> None:
    """Manual add restores a REMOVED stakeholder instead of creating a duplicate."""
    from src.api.schemas.crm_models import CreateStakeholderRequest
    from src.domain.models.organisation_tables.sync.crm.stakeholders import (
        DecisionPower,
        StakeholderStance,
    )

    org_id = uuid4()
    deal_id = uuid4()
    removed = DealStakeholder(
        organisation_id=org_id,
        deal_id=deal_id,
        email="buyer@customer.com",
        name="Old Buyer",
        job_title="Old Title",
        source=StakeholderSource.MANUAL,
        approval_status=ApprovalStatus.REMOVED,
    )
    session = _FakeSession(org_domains=["mycompany.com"])
    session.existing_stakeholder = removed
    session.executed_statements = []  # type: ignore[attr-defined]

    async def execute(stmt):
        """Record executed statements and return fake rows for this test case."""
        compiled = str(stmt).lower()
        session.executed_statements.append(compiled)  # type: ignore[attr-defined]
        if "organisations" in compiled or "domain" in compiled:
            return _FakeScalarResult(session.org_domains)
        if "deal_stakeholders" in compiled:
            return _FakeScalarResult(session.existing_stakeholder)
        return _FakeScalarResult(None)

    session.execute = execute  # type: ignore[method-assign]
    session.added = []  # type: ignore[attr-defined]
    session.committed = False  # type: ignore[attr-defined]

    def add(obj):
        """Record staged model objects for assertions."""
        session.added.append(obj)  # type: ignore[attr-defined]

    async def commit():
        """Mark commit status on the fake session."""
        session.committed = True  # type: ignore[attr-defined]

    session.add = add  # type: ignore[method-assign]
    session.commit = commit  # type: ignore[method-assign]

    service = StakeholdersService(org_session=session, organisation_id=org_id)  # type: ignore[arg-type]
    service._is_internal_email = AsyncMock(return_value=False)  # type: ignore[method-assign]
    service._ensure_contact_for_stakeholder = AsyncMock(return_value=uuid4())  # type: ignore[method-assign]
    service._ensure_deal_participant = AsyncMock(return_value=True)  # type: ignore[method-assign]

    result = await service.create_stakeholder(
        deal_id=deal_id,
        request=CreateStakeholderRequest(
            email="buyer@customer.com",
            name="Restored Buyer",
            job_title="New Title",
            stance=StakeholderStance.UNKNOWN,
            decision_power=DecisionPower.UNKNOWN,
        ),
    )

    assert result.stakeholder_id == removed.stakeholder_id
    assert removed.approval_status == ApprovalStatus.APPROVED
    assert removed.name == "Restored Buyer"
    assert removed.job_title == "New Title"
    assert session.committed is True  # type: ignore[attr-defined]
    timeline_cleanup = [
        stmt
        for stmt in session.executed_statements  # type: ignore[attr-defined]
        if "delete from deal_timeline_events" in stmt
    ]
    assert len(timeline_cleanup) == 2


@pytest.mark.asyncio
async def test_create_stakeholder_duplicate_active_error_includes_conflict_data() -> None:
    """Duplicate active stakeholders return metadata that the API can serialize."""
    from src.api.schemas.crm_models import CreateStakeholderRequest
    from src.domain.models.organisation_tables.sync.crm.stakeholders import (
        DecisionPower,
        StakeholderStance,
    )

    org_id = uuid4()
    deal_id = uuid4()
    active = DealStakeholder(
        organisation_id=org_id,
        deal_id=deal_id,
        email="buyer@customer.com",
        approval_status=ApprovalStatus.APPROVED,
    )
    session = _FakeSession(org_domains=["mycompany.com"])

    async def execute(stmt):
        """Return canned scalar rows for duplicate-check branch assertions."""
        compiled = str(stmt).lower()
        if "organisations" in compiled or "domain" in compiled:
            return _FakeScalarResult(session.org_domains)
        if "deal_stakeholders" in compiled:
            return _FakeScalarResult(active)
        return _FakeScalarResult(None)

    session.execute = execute  # type: ignore[method-assign]
    service = StakeholdersService(org_session=session, organisation_id=org_id)  # type: ignore[arg-type]
    service._is_internal_email = AsyncMock(return_value=False)  # type: ignore[method-assign]

    with pytest.raises(StakeholdersServiceError) as exc_info:
        await service.create_stakeholder(
            deal_id=deal_id,
            request=CreateStakeholderRequest(
                email="buyer@customer.com",
                stance=StakeholderStance.UNKNOWN,
                decision_power=DecisionPower.UNKNOWN,
            ),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.data == {
        "stakeholder_id": str(active.stakeholder_id),
        "approval_status": "APPROVED",
        "is_deleted": False,
    }
