"""Tests for shared deal-mapping eligibility decisions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import src.workers.deal_worker.services.deal_mapper_services.deal_mapping_eligibility as eligibility_module
from sqlalchemy.dialects import postgresql
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealStatus,
    CRMParticipantType,
)
from src.workers.deal_worker.services.deal_mapper_services.deal_mapping_eligibility import (
    DealMappingEligibilityService,
    DealMappingSkipReason,
)


def _deal(**overrides: object) -> SimpleNamespace:
    defaults = {
        "deal_id": uuid4(),
        "organisation_id": uuid4(),
        "deal_status": CRMDealStatus.OPEN,
        "is_deleted": False,
        "owner_email": None,
        "participants": [],
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _participant(**overrides: object) -> SimpleNamespace:
    defaults = {
        "participant_type": CRMParticipantType.USER,
        "related_email": "seller@example.com",
        "is_deleted": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_owner_email_in_public_users_makes_deal_eligible() -> None:
    organisation_id = uuid4()
    deal = _deal(organisation_id=organisation_id, owner_email=" Owner@Example.com ")
    service = DealMappingEligibilityService(organisation_id)

    result = service.evaluate_loaded_deal(deal, public_user_emails={"owner@example.com"})

    assert result.is_eligible is True
    assert result.reason is None
    assert result.internal_user_emails == ["owner@example.com"]
    assert result.missing_public_user_emails == []


def test_crm_only_owner_email_is_rejected() -> None:
    organisation_id = uuid4()
    deal = _deal(organisation_id=organisation_id, owner_email="crm-only@example.com")
    service = DealMappingEligibilityService(organisation_id)

    result = service.evaluate_loaded_deal(deal, public_user_emails=set())

    assert result.is_eligible is False
    assert result.reason == DealMappingSkipReason.NO_APP_USER_ON_DEAL
    assert result.missing_public_user_emails == ["crm-only@example.com"]


@pytest.mark.parametrize(
    ("deal_status", "is_deleted", "expected_reason"),
    [
        (CRMDealStatus.WON, False, DealMappingSkipReason.DEAL_NOT_OPEN),
        (CRMDealStatus.OPEN, True, DealMappingSkipReason.DEAL_DELETED),
    ],
)
def test_closed_or_deleted_deal_is_rejected(
    deal_status: CRMDealStatus,
    is_deleted: bool,
    expected_reason: DealMappingSkipReason,
) -> None:
    organisation_id = uuid4()
    deal = _deal(
        organisation_id=organisation_id,
        deal_status=deal_status,
        is_deleted=is_deleted,
        owner_email="owner@example.com",
    )
    service = DealMappingEligibilityService(organisation_id)

    result = service.evaluate_loaded_deal(deal, public_user_emails={"owner@example.com"})

    assert result.is_eligible is False
    assert result.reason == expected_reason


def test_deal_without_internal_user_emails_is_rejected() -> None:
    organisation_id = uuid4()
    deal = _deal(
        organisation_id=organisation_id,
        participants=[
            _participant(
                participant_type=CRMParticipantType.CONTACT,
                related_email="buyer@example.com",
            )
        ],
    )
    service = DealMappingEligibilityService(organisation_id)

    result = service.evaluate_loaded_deal(deal, public_user_emails={"buyer@example.com"})

    assert result.is_eligible is False
    assert result.reason == DealMappingSkipReason.NO_INTERNAL_USERS


def test_crm_only_user_participant_does_not_block_app_user_owner() -> None:
    organisation_id = uuid4()
    deal = _deal(
        organisation_id=organisation_id,
        owner_email="owner@example.com",
        participants=[
            _participant(related_email=" teammate@example.com "),
            _participant(related_email="deleted@example.com", is_deleted=True),
        ],
    )
    service = DealMappingEligibilityService(organisation_id)

    result = service.evaluate_loaded_deal(deal, public_user_emails={"owner@example.com"})

    assert result.is_eligible is True
    assert result.reason is None
    assert result.internal_user_emails == ["owner@example.com", "teammate@example.com"]


def test_any_app_user_on_deal_is_enough_for_eligibility() -> None:
    organisation_id = uuid4()
    deal = _deal(
        organisation_id=organisation_id,
        owner_email="crm-only-owner@example.com",
        participants=[
            _participant(related_email=" app-user@example.com "),
            _participant(related_email="crm-only-user@example.com"),
        ],
    )
    service = DealMappingEligibilityService(organisation_id)

    result = service.evaluate_loaded_deal(deal, public_user_emails={"app-user@example.com"})

    assert result.is_eligible is True
    assert result.reason is None
    assert result.internal_user_emails == [
        "crm-only-owner@example.com",
        "app-user@example.com",
        "crm-only-user@example.com",
    ]


def test_deal_with_internal_users_but_no_app_user_is_rejected() -> None:
    organisation_id = uuid4()
    deal = _deal(
        organisation_id=organisation_id,
        owner_email="crm-only-owner@example.com",
        participants=[_participant(related_email="crm-only-user@example.com")],
    )
    service = DealMappingEligibilityService(organisation_id)

    result = service.evaluate_loaded_deal(deal, public_user_emails={"someone-else@example.com"})

    assert result.is_eligible is False
    assert result.reason == DealMappingSkipReason.NO_APP_USER_ON_DEAL


def test_list_eligible_loaded_deal_ids_returns_only_eligible_deals() -> None:
    organisation_id = uuid4()
    eligible_deal_id = uuid4()
    crm_only_deal_id = uuid4()
    closed_deal_id = uuid4()
    service = DealMappingEligibilityService(organisation_id)
    deals = [
        _deal(
            deal_id=eligible_deal_id,
            organisation_id=organisation_id,
            owner_email="app@example.com",
        ),
        _deal(
            deal_id=crm_only_deal_id,
            organisation_id=organisation_id,
            owner_email="crm-only@example.com",
        ),
        _deal(
            deal_id=closed_deal_id,
            organisation_id=organisation_id,
            deal_status=CRMDealStatus.CLOSED,
            owner_email="app@example.com",
        ),
    ]

    result = service.list_eligible_loaded_deal_ids(
        deals,
        public_user_emails={"app@example.com"},
    )

    assert result == [eligible_deal_id]


def test_open_deals_query_filters_org_open_and_not_deleted() -> None:
    organisation_id = uuid4()
    service = DealMappingEligibilityService(organisation_id)

    stmt = service.build_open_deals_query()

    compiled_query = str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert f"crm_deals.organisation_id = '{organisation_id}'" in compiled_query
    assert "crm_deals.deal_status = 'open'" in compiled_query
    assert "crm_deals.is_deleted IS false" in compiled_query
    assert "ORDER BY crm_deals.deal_id" in compiled_query


def test_open_deals_query_applies_optional_limit() -> None:
    organisation_id = uuid4()
    service = DealMappingEligibilityService(organisation_id)

    stmt = service.build_open_deals_query(limit=1001)

    compiled_query = str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "LIMIT 1001" in compiled_query


def test_open_deals_query_excludes_existing_claims_before_limit() -> None:
    organisation_id = uuid4()
    excluded_deal_id = uuid4()
    service = DealMappingEligibilityService(organisation_id)

    stmt = service.build_open_deals_query(
        exclude_deal_ids={excluded_deal_id},
        limit=1001,
    )

    compiled_query = str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert f"crm_deals.deal_id NOT IN ('{excluded_deal_id}')" in compiled_query
    assert compiled_query.index("crm_deals.deal_id NOT IN") < compiled_query.index("LIMIT 1001")


@pytest.mark.asyncio
async def test_list_eligible_deal_ids_uses_org_deals_and_public_users(monkeypatch) -> None:
    organisation_id = uuid4()
    eligible_deal_id = uuid4()
    ineligible_deal_id = uuid4()
    deals = [
        _deal(
            deal_id=eligible_deal_id,
            organisation_id=organisation_id,
            owner_email="app@example.com",
        ),
        _deal(
            deal_id=ineligible_deal_id,
            organisation_id=organisation_id,
            participants=[_participant(related_email="crm-only@example.com")],
        ),
    ]
    org_session = _FakeOrgSession(deals)
    master_session = _FakeMasterSession(rows=[("app@example.com",)])
    monkeypatch.setattr(
        eligibility_module,
        "acquire_org_async_session",
        _session_generator_factory([org_session]),
    )
    monkeypatch.setattr(
        eligibility_module,
        "acquire_master_async_session",
        _session_generator_factory([master_session]),
    )
    service = DealMappingEligibilityService(organisation_id)

    result = await service.list_eligible_deal_ids()

    assert result == [eligible_deal_id]
    org_session.execute.assert_awaited_once()
    master_session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_eligible_deal_ids_with_scan_metrics_caps_candidates(monkeypatch) -> None:
    organisation_id = uuid4()
    eligible_deal_id = uuid4()
    beyond_limit_deal_id = uuid4()
    deals = [
        _deal(
            deal_id=eligible_deal_id,
            organisation_id=organisation_id,
            owner_email="app@example.com",
        ),
        _deal(
            deal_id=beyond_limit_deal_id,
            organisation_id=organisation_id,
            owner_email="app@example.com",
        ),
    ]
    org_session = _FakeOrgSession(deals)
    master_session = _FakeMasterSession(rows=[("app@example.com",)])
    monkeypatch.setattr(
        eligibility_module,
        "acquire_org_async_session",
        _session_generator_factory([org_session]),
    )
    monkeypatch.setattr(
        eligibility_module,
        "acquire_master_async_session",
        _session_generator_factory([master_session]),
    )
    service = DealMappingEligibilityService(organisation_id)

    result = await service.list_eligible_deal_ids_with_scan_metrics(candidate_scan_limit=1)

    assert result.eligible_deal_ids == [eligible_deal_id]
    assert result.candidate_deals_scanned == 1
    assert result.candidate_scan_limit == 1
    assert result.candidate_scan_capped is True


class _FakeScalarResult:
    def __init__(self, rows: list[SimpleNamespace]):
        self._rows = rows

    def all(self) -> list[SimpleNamespace]:
        return self._rows


class _FakeOrgExecuteResult:
    def __init__(self, rows: list[SimpleNamespace]):
        self._rows = rows

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._rows)


class _FakeOrgSession:
    def __init__(self, rows: list[SimpleNamespace]):
        self.execute = AsyncMock(return_value=_FakeOrgExecuteResult(rows))


class _FakeMasterExecuteResult:
    def __init__(self, rows: list[tuple[str]]):
        self._rows = rows

    def all(self) -> list[tuple[str]]:
        return self._rows


class _FakeMasterSession:
    def __init__(self, rows: list[tuple[str]]):
        self.execute = AsyncMock(return_value=_FakeMasterExecuteResult(rows))


def _session_generator_factory(sessions: list[object]):
    queue = iter(sessions)

    def _factory(*_args: object) -> AsyncIterator[object]:
        async def _generator() -> AsyncIterator[object]:
            yield next(queue)

        return _generator()

    return _factory
