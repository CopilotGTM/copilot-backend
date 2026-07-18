"""Regression tests for relationship-based deal access decisions."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.master_tables.user_models import UserAppRole
from src.service.crm.access.record_access_policy import (
    CRMAccessActor,
    CRMSystemActor,
    DealAccessPolicy,
)


class _ScalarSession:
    def __init__(self, values: list[bool]):
        self.values = iter(values)

    async def scalar(self, _statement):
        """Return deterministic existence checks in policy evaluation order."""
        return next(self.values)


@pytest.mark.asyncio
async def test_unassigned_creator_can_control_deal() -> None:
    """An unassigned deal remains controllable by its creator."""
    actor_id = uuid4()
    policy = DealAccessPolicy(
        _ScalarSession([True, False]),  # type: ignore[arg-type]
        uuid4(),
        CRMAccessActor(actor_id, UserAppRole.MEMBER),
    )
    deal = SimpleNamespace(
        deal_id=uuid4(),
        owner_id=None,
        owner_email=None,
        created_by_user_id=None,
    )

    decision = await policy.decision_for_deal(deal)  # type: ignore[arg-type]

    assert decision.is_creator is True
    assert decision.can_view is True
    assert decision.can_collaborate is True
    assert decision.can_control is True


@pytest.mark.asyncio
async def test_creator_becomes_read_only_after_ownership_transfer() -> None:
    """Creator visibility persists after transfer without preserving mutation rights."""
    actor_id = uuid4()
    policy = DealAccessPolicy(
        _ScalarSession([False]),  # type: ignore[arg-type]
        uuid4(),
        CRMAccessActor(actor_id, UserAppRole.MEMBER),
    )
    deal = SimpleNamespace(
        deal_id=uuid4(),
        owner_id=uuid4(),
        owner_email=None,
        created_by_user_id=actor_id,
    )

    decision = await policy.decision_for_deal(deal)  # type: ignore[arg-type]

    assert decision.can_view is True
    assert decision.can_collaborate is False
    assert decision.can_control is False


@pytest.mark.asyncio
async def test_admin_has_all_deal_capabilities() -> None:
    """Admins retain every capability without relationship lookups."""
    policy = DealAccessPolicy(
        _ScalarSession([]),  # type: ignore[arg-type]
        uuid4(),
        CRMAccessActor(uuid4(), UserAppRole.ADMIN),
    )

    decision = await policy.decision_for_deal(SimpleNamespace())  # type: ignore[arg-type]

    assert decision.can_view
    assert decision.can_collaborate
    assert decision.can_control
    assert decision.can_administer


@pytest.mark.asyncio
async def test_missing_actor_fails_closed() -> None:
    """Omitting a principal must deny both loaded-record and SQL visibility."""
    policy = DealAccessPolicy(
        _ScalarSession([]),  # type: ignore[arg-type]
        uuid4(),
        None,
    )

    decision = await policy.decision_for_deal(SimpleNamespace())  # type: ignore[arg-type]

    assert decision.can_view is False
    assert decision.can_collaborate is False
    assert decision.can_control is False
    assert decision.can_administer is False
    assert str(policy.visible_deal_predicate()) == "false"


@pytest.mark.asyncio
async def test_explicit_system_actor_has_org_wide_access() -> None:
    """Trusted workers can deliberately request organization-wide authority."""
    policy = DealAccessPolicy(
        _ScalarSession([]),  # type: ignore[arg-type]
        uuid4(),
        CRMSystemActor("crm_sync"),
    )

    decision = await policy.decision_for_deal(SimpleNamespace())  # type: ignore[arg-type]

    assert decision.can_view
    assert decision.can_collaborate
    assert decision.can_control
    assert decision.can_administer
