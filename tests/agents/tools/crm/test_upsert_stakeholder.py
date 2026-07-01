from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from src.agents.context import AgentContext
from src.agents.tools.crm.upsert_stakeholder import (
    UpsertStakeholderInput,
    upsert_stakeholder,
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
async def test_upsert_stakeholder_create_path_uses_participant_id_for_research() -> None:
    context = _context()
    deal_id = uuid4()
    participant_id = uuid4()
    stakeholder = _participant_only_stakeholder(participant_id)

    with (
        patch(
            "src.agents.tools.crm.upsert_stakeholder.is_internal_email",
            AsyncMock(return_value=False),
        ),
        patch("src.agents.tools.crm.upsert_stakeholder.StakeholderWriteService") as service_cls,
        patch(
            "src.workers.deal_worker.utils.stakeholder_research_dispatch.dispatch_stakeholder_research_async",
            AsyncMock(return_value=uuid4()),
        ) as research_dispatch,
    ):
        service = MagicMock()
        service._find_contact_participant = AsyncMock(return_value=None)
        service.create_stakeholder = AsyncMock(return_value=stakeholder)
        service_cls.return_value = service

        result = await upsert_stakeholder(
            context=context,
            deal_id=str(deal_id),
            input_data=UpsertStakeholderInput(
                email="buyer@acme.com",
                name="Buyer Person",
                job_title="VP Sales",
            ),
        )

    research_dispatch.assert_awaited_once_with(
        organisation_id=context.organisation_id,
        stakeholder_id=participant_id,
        deal_id=deal_id,
    )
    assert result is not None
    assert result["action"] == "created"
    assert result["participant_id"] == str(participant_id)
    assert "stakeholder_id" not in result


@pytest.mark.asyncio
async def test_upsert_stakeholder_create_path_dispatches_contact_research_for_contact_backed_create() -> (
    None
):
    """Upsert-created contact-backed stakeholders should enqueue contact research."""
    context = _context()
    deal_id = uuid4()
    participant_id = uuid4()
    contact_id = uuid4()
    stakeholder = _participant_only_stakeholder(participant_id)
    stakeholder.contact_id = contact_id

    with (
        patch(
            "src.agents.tools.crm.upsert_stakeholder.is_internal_email",
            AsyncMock(return_value=False),
        ),
        patch("src.agents.tools.crm.upsert_stakeholder.StakeholderWriteService") as service_cls,
        patch(
            "src.workers.deal_worker.utils.contact_research_dispatch."
            "dispatch_contact_research_async",
            AsyncMock(return_value=str(uuid4())),
        ) as contact_research_dispatch,
        patch(
            "src.workers.deal_worker.utils.stakeholder_research_dispatch."
            "dispatch_stakeholder_research_async",
            AsyncMock(return_value=uuid4()),
        ) as stakeholder_research_dispatch,
    ):
        service = MagicMock()
        service._find_contact_participant = AsyncMock(return_value=None)
        service.create_stakeholder = AsyncMock(return_value=stakeholder)
        service_cls.return_value = service

        result = await upsert_stakeholder(
            context=context,
            deal_id=str(deal_id),
            input_data=UpsertStakeholderInput(
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
    stakeholder_research_dispatch.assert_not_awaited()
    assert result is not None
    assert result["action"] == "created"
