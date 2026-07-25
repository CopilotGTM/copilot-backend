"""Focused deal-participant trigger contract tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import Response as FastAPIResponse
from src.api.routers.crm.deal_participants_handler import (
    add_deal_participants,
    remove_deal_participant,
)
from src.api.schemas.crm_participant_models import DealParticipantsCreateRequest
from src.domain.models.organisation_tables.sync.crm.deals import CRMParticipantType
from src.service.crm.participants.contracts import ParticipantWriteResult


def _participant(deal_id, contact_id):
    return SimpleNamespace(
        participant_id=uuid4(),
        deal_id=deal_id,
        participant_type=CRMParticipantType.CONTACT,
        related_local_id=contact_id,
        related_name="Buyer",
        related_email="buyer@example.com",
        relationship_role=None,
        is_deleted=False,
    )


@pytest.mark.asyncio
async def test_add_deal_participant_publishes_participant_added_trigger(monkeypatch) -> None:
    """A committed contact participant should enqueue the stable added value."""
    org_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    participant = _participant(deal_id, contact_id)
    session = SimpleNamespace(
        commit=AsyncMock(),
        refresh=AsyncMock(),
        rollback=AsyncMock(),
    )
    service = SimpleNamespace(
        organisation_id=org_id,
        session=session,
        add_participants=AsyncMock(
            return_value=ParticipantWriteResult(
                created=[participant],
                skipped=[],
                contact_participant_changed=True,
                integration_id=uuid4(),
            )
        ),
    )
    dispatch = AsyncMock()
    monkeypatch.setattr(
        "src.workers.deal_worker.client.send_deal_task_async",
        dispatch,
    )

    result = await add_deal_participants(
        deal_id=deal_id,
        request=DealParticipantsCreateRequest(
            participants=[
                {
                    "participant_type": CRMParticipantType.CONTACT,
                    "related_id": contact_id,
                }
            ]
        ),
        response=FastAPIResponse(),
        service=service,
        _crm_write_guard=None,
        _rl_write=None,
    )

    assert result.status == "SUCCESS"
    session.commit.assert_awaited_once()
    dispatch.assert_awaited_once_with(
        "stakeholder.sync_deal_stakeholders",
        kwargs={
            "organisation_id": str(org_id),
            "deal_id": str(deal_id),
            "change_type": "PARTICIPANT_ADDED",
            "integration_id": None,
            "integration_type": None,
            "trigger_rematching": True,
        },
    )


@pytest.mark.asyncio
async def test_remove_deal_participant_publishes_participant_removed_trigger(monkeypatch) -> None:
    """Removing a contact participant should enqueue the stable removed value."""
    org_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    participant = _participant(deal_id, contact_id)
    session = SimpleNamespace(
        commit=AsyncMock(),
        refresh=AsyncMock(),
        rollback=AsyncMock(),
    )
    service = SimpleNamespace(
        organisation_id=org_id,
        session=session,
        remove_participant=AsyncMock(return_value=(participant, True, uuid4())),
    )
    dispatch = AsyncMock()
    monkeypatch.setattr(
        "src.workers.deal_worker.client.send_deal_task_async",
        dispatch,
    )

    result = await remove_deal_participant(
        deal_id=deal_id,
        participant_id=participant.participant_id,
        response=FastAPIResponse(),
        service=service,
        _crm_write_guard=None,
        _rl_write=None,
    )

    assert result.status == "SUCCESS"
    session.commit.assert_awaited_once()
    dispatch.assert_awaited_once_with(
        "stakeholder.sync_deal_stakeholders",
        kwargs={
            "organisation_id": str(org_id),
            "deal_id": str(deal_id),
            "change_type": "PARTICIPANT_REMOVED",
            "integration_id": None,
            "integration_type": None,
            "trigger_rematching": True,
        },
    )
