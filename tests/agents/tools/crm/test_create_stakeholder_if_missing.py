from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from src.agents.context import AgentContext
from src.agents.tools.crm.create_stakeholder_if_missing import (
    CreateStakeholderIfMissingInput,
    create_stakeholder_if_missing,
)


class _AsyncSessionContext:
    def __init__(self, session: MagicMock) -> None:
        self.session = session

    async def __aenter__(self) -> MagicMock:
        return self.session

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _context() -> AgentContext:
    session = MagicMock()
    session.commit = AsyncMock()
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        user_email="seller@copilotgtm.com",
        channel="web",
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))
    return context


def _participant_only_stakeholder(participant_id: UUID) -> SimpleNamespace:
    return SimpleNamespace(
        participant_id=participant_id,
        contact_id=None,
        email="buyer@acme.com",
        name="Buyer Person",
        job_title="VP Sales",
        phone_number="+1 555 0100",
        department="Sales",
        stance="CHAMPION",
        decision_power="DECISION_MAKER",
        temperature="WARM",
        motivation="Expansion",
        pain_points=["Manual reporting"],
    )


@pytest.mark.asyncio
async def test_create_stakeholder_if_missing_without_contact_skips_research() -> None:
    context = _context()
    deal_id = uuid4()
    participant_id = uuid4()
    stakeholder = _participant_only_stakeholder(participant_id)

    with (
        patch(
            "src.agents.tools.crm.create_stakeholder_if_missing.is_internal_email",
            AsyncMock(return_value=False),
        ),
        patch(
            "src.agents.tools.crm.create_stakeholder_if_missing.StakeholderWriteService"
        ) as service_cls,
        patch(
            "src.agents.tools.crm.create_stakeholder_if_missing.DealJobService"
        ) as job_service_cls,
    ):
        service = MagicMock()
        service._find_contact_participant = AsyncMock(return_value=None)
        service.create_stakeholder_with_outcome = AsyncMock(
            return_value=SimpleNamespace(
                stakeholder=stakeholder,
                created=True,
                reactivated=False,
                reused=False,
            )
        )
        service_cls.return_value = service
        job_service = MagicMock()
        job_service.create_stakeholder_processing_jobs = AsyncMock(return_value=[])
        job_service_cls.return_value = job_service

        result = await create_stakeholder_if_missing(
            context=context,
            deal_id=str(deal_id),
            input_data=CreateStakeholderIfMissingInput(
                email="buyer@acme.com",
                name="Buyer Person",
                job_title="VP Sales",
            ),
        )

    assert result is not None
    assert result["action"] == "created"
    assert result["participant_id"] == str(participant_id)
    assert result["deal_id"] == str(deal_id)
    assert "stakeholder_id" not in result
    job_service.create_stakeholder_processing_jobs.assert_awaited_once_with(
        deal_id,
        participant_id,
    )


@pytest.mark.asyncio
async def test_create_stakeholder_if_missing_dispatches_contact_research_for_contact_backed_create() -> (
    None
):
    """Create-only agent writes should follow contact research when a contact was linked."""
    context = _context()
    deal_id = uuid4()
    participant_id = uuid4()
    contact_id = uuid4()
    stakeholder = _participant_only_stakeholder(participant_id)
    stakeholder.contact_id = contact_id

    with (
        patch(
            "src.agents.tools.crm.create_stakeholder_if_missing.is_internal_email",
            AsyncMock(return_value=False),
        ),
        patch(
            "src.agents.tools.crm.create_stakeholder_if_missing.StakeholderWriteService"
        ) as service_cls,
        patch(
            "src.agents.tools.crm.create_stakeholder_if_missing.DealJobService"
        ) as job_service_cls,
        patch(
            "src.agents.tools.crm.create_stakeholder_if_missing."
            "dispatch_contact_research_for_contact",
            AsyncMock(return_value=str(uuid4())),
        ) as contact_research_dispatch,
    ):
        service = MagicMock()
        service._find_contact_participant = AsyncMock(return_value=None)
        service.create_stakeholder_with_outcome = AsyncMock(
            return_value=SimpleNamespace(
                stakeholder=stakeholder,
                created=True,
                reactivated=False,
                reused=False,
            )
        )
        service_cls.return_value = service
        job_service = MagicMock()
        job_service.create_stakeholder_processing_jobs = AsyncMock(return_value=[])
        job_service_cls.return_value = job_service

        result = await create_stakeholder_if_missing(
            context=context,
            deal_id=str(deal_id),
            input_data=CreateStakeholderIfMissingInput(
                email="buyer@acme.com",
                name="Buyer Person",
                job_title="VP Sales",
            ),
        )

    contact_research_dispatch.assert_awaited_once_with(
        organisation_id=context.organisation_id,
        contact_id=contact_id,
        user_id=context.user_id,
        trigger="stakeholder_created",
    )
    assert result is not None
    assert result["action"] == "created"


@pytest.mark.asyncio
async def test_create_stakeholder_if_missing_reactivates_and_replays_soft_deleted_match() -> None:
    """A removed stakeholder is missing from the deal and must re-enter the unified flow."""
    context = _context()
    deal_id = uuid4()
    stakeholder = _participant_only_stakeholder(uuid4())

    with (
        patch(
            "src.agents.tools.crm.create_stakeholder_if_missing.is_internal_email",
            AsyncMock(return_value=False),
        ),
        patch(
            "src.agents.tools.crm.create_stakeholder_if_missing.StakeholderWriteService"
        ) as service_cls,
        patch(
            "src.agents.tools.crm.create_stakeholder_if_missing.DealJobService"
        ) as job_service_cls,
        patch(
            "src.agents.tools.crm.create_stakeholder_if_missing."
            "dispatch_contact_research_for_contact",
            AsyncMock(return_value=None),
        ),
    ):
        service = MagicMock()
        service._find_contact_participant = AsyncMock(return_value=None)
        service.create_stakeholder_with_outcome = AsyncMock(
            return_value=SimpleNamespace(
                stakeholder=stakeholder,
                created=False,
                reactivated=True,
                reused=False,
            )
        )
        service_cls.return_value = service
        job_service = MagicMock()
        job_service.create_stakeholder_processing_jobs = AsyncMock(return_value=[])
        job_service_cls.return_value = job_service

        result = await create_stakeholder_if_missing(
            context=context,
            deal_id=str(deal_id),
            input_data=CreateStakeholderIfMissingInput(email=stakeholder.email),
        )

    service._find_contact_participant.assert_awaited_once_with(
        deal_id=deal_id,
        email=stakeholder.email,
        include_deleted=False,
    )
    job_service.create_stakeholder_processing_jobs.assert_awaited_once_with(
        deal_id,
        stakeholder.participant_id,
    )
    assert result is not None
    assert result["action"] == "reactivated"


@pytest.mark.asyncio
async def test_create_stakeholder_if_missing_reports_reused_race_as_existing() -> None:
    """A concurrent identity reuse must not be reported or replayed as a new create."""
    context = _context()
    deal_id = uuid4()
    stakeholder = _participant_only_stakeholder(uuid4())

    with (
        patch(
            "src.agents.tools.crm.create_stakeholder_if_missing.is_internal_email",
            AsyncMock(return_value=False),
        ),
        patch(
            "src.agents.tools.crm.create_stakeholder_if_missing.StakeholderWriteService"
        ) as service_cls,
        patch(
            "src.agents.tools.crm.create_stakeholder_if_missing.DealJobService"
        ) as job_service_cls,
        patch(
            "src.agents.tools.crm.create_stakeholder_if_missing."
            "dispatch_contact_research_for_contact",
            AsyncMock(return_value=None),
        ) as research_dispatch,
    ):
        service = MagicMock()
        service._find_contact_participant = AsyncMock(return_value=None)
        service.create_stakeholder_with_outcome = AsyncMock(
            return_value=SimpleNamespace(
                stakeholder=stakeholder,
                created=False,
                reactivated=False,
                reused=True,
            )
        )
        service_cls.return_value = service
        job_service = MagicMock()
        job_service.create_stakeholder_processing_jobs = AsyncMock(return_value=[])
        job_service_cls.return_value = job_service

        result = await create_stakeholder_if_missing(
            context=context,
            deal_id=str(deal_id),
            input_data=CreateStakeholderIfMissingInput(email=stakeholder.email),
        )

    assert result is not None
    assert result["action"] == "already_exists"
    assert result["created"] is False
    job_service.create_stakeholder_processing_jobs.assert_not_awaited()
    research_dispatch.assert_not_awaited()
