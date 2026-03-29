"""Tests for internal email guard in StakeholdersService.create_stakeholder."""

from __future__ import annotations

from uuid import uuid4

import pytest
from src.service.crm.insights.stakeholders_service import (
    StakeholdersService,
    StakeholdersServiceError,
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
    """Minimal session that returns canned Organisation.domain values."""

    def __init__(self, org_domains: list[str] | None = None):
        self.org_domains = org_domains

    async def execute(self, stmt):
        compiled = str(stmt)
        if "organisations" in compiled.lower() or "domain" in compiled.lower():
            return _FakeScalarResult(self.org_domains)
        # For the duplicate-check query, return None (no existing stakeholder)
        return _FakeScalarResult(None)

    async def commit(self):
        pass

    async def rollback(self):
        pass

    def add(self, obj):
        pass

    async def refresh(self, obj):
        pass


# ---------------------------------------------------------------------------
# Tests: _is_internal_email
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_internal_email_matches_org_domain() -> None:
    """Email whose domain matches Organisation.domain is internal."""
    session = _FakeSession(org_domains=["acme.com", "acme.io"])
    service = StakeholdersService(org_session=session, organisation_id=uuid4())  # type: ignore[arg-type]

    assert await service._is_internal_email("seller@acme.com") is True
    assert await service._is_internal_email("TEAM@Acme.IO") is True


@pytest.mark.asyncio
async def test_is_internal_email_allows_external() -> None:
    """External emails are not flagged as internal."""
    session = _FakeSession(org_domains=["acme.com"])
    service = StakeholdersService(org_session=session, organisation_id=uuid4())  # type: ignore[arg-type]

    assert await service._is_internal_email("buyer@customer.com") is False
    assert await service._is_internal_email("prospect@external.org") is False


@pytest.mark.asyncio
async def test_is_internal_email_no_org_domains() -> None:
    """When no org domains configured, nothing is blocked."""
    session = _FakeSession(org_domains=None)
    service = StakeholdersService(org_session=session, organisation_id=uuid4())  # type: ignore[arg-type]

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

    request = CreateStakeholderRequest(
        email="buyer@customer.com",
        name="External Buyer",
        stance=StakeholderStance.UNKNOWN,
        decision_power=DecisionPower.UNKNOWN,
    )

    # Should NOT raise 403 — it will fail later because the fake session can't
    # create real DB objects, but that's fine for this test.
    try:
        await service.create_stakeholder(deal_id=uuid4(), request=request)
    except StakeholdersServiceError as e:
        # 409 (duplicate) or other errors are fine — just NOT 403
        assert e.status_code != 403, f"External email should not be rejected: {e.message}"
    except Exception:  # noqa: S110
        # Any non-StakeholdersServiceError is OK (fake session limitations)
        pass
