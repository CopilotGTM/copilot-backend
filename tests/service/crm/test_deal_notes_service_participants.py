from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import ApprovalStatus
from src.service.crm.internal.deal_notes_service import DealNotesService


class _ScalarRows:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows


class _ExecuteRows:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarRows:
        return _ScalarRows(self._rows)


class _NotesSession:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.statements = []

    async def execute(self, stmt):  # noqa: ANN001
        self.statements.append(stmt)
        return _ExecuteRows(self.rows)


@pytest.mark.asyncio
async def test_deal_note_mentions_use_active_approved_contact_participants() -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    participant = CRMDealParticipant(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        deal_id=deal_id,
        participant_type=CRMParticipantType.CONTACT,
        related_email="buyer@example.com",
        related_name="Buyer One",
        approval_status=ApprovalStatus.APPROVED.value,
    )
    session = _NotesSession([participant])
    service = DealNotesService(session, organisation_id)
    deal = SimpleNamespace(owner_email="owner@example.com", owner_name="Owner")

    participants = await service._get_deal_participants(deal_id, deal)  # noqa: SLF001

    assert [(p.email, p.name) for p in participants] == [
        ("owner@example.com", "Owner"),
        ("buyer@example.com", "Buyer One"),
    ]
    compiled = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "crm_deal_participants" in compiled
    assert "deal_stakeholders" not in compiled
