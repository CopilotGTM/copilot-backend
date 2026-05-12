from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.service.crm.access.deals_access_service import DealsAccessService


class _FakeRowsResult:
    def __init__(self, rows: list[object] | None = None) -> None:
        self.statement = None
        self._rows = rows or []

    def all(self) -> list[object]:
        return self._rows


class _CapturingSession:
    def __init__(self, rows: list[object] | None = None) -> None:
        self.result = _FakeRowsResult(rows)

    async def execute(self, statement):
        self.result.statement = statement
        return self.result


@pytest.mark.asyncio
async def test_get_deal_team_members_batch_keeps_unroled_user_participants() -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    participant_id = uuid4()
    user_id = uuid4()
    session = _CapturingSession(
        [
            SimpleNamespace(
                participant_id=participant_id,
                deal_id=deal_id,
                related_local_id=user_id,
                related_name="Avery Seller",
                related_email="avery@example.com",
                relationship_role=None,
            )
        ]
    )
    service = DealsAccessService(session, organisation_id)  # type: ignore[arg-type]

    result = await service.get_deal_team_members_batch([deal_id])

    compiled = str(session.result.statement).lower()
    assert "coalesce" in compiled
    assert "!=" in compiled
    assert result == {
        deal_id: [
            {
                "participant_id": participant_id,
                "user_id": user_id,
                "name": "Avery Seller",
                "email": "avery@example.com",
                "role": "member",
            }
        ]
    }
