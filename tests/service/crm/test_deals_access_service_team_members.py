"""Regression tests for deal team member read-shape boundaries."""

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
        """Return fake DB rows for the access service mapper."""
        return self._rows


class _CapturingSession:
    def __init__(self, rows: list[object] | None = None) -> None:
        self._rows = rows or []
        self.result = _FakeRowsResult(self._rows)

    async def execute(self, statement):
        """Capture the generated SQL statement while returning fake rows."""
        compiled = str(statement).lower()
        rows = self._rows
        if "lower" in compiled and "trim" in compiled and "replace" in compiled:
            rows = [
                row
                for row in rows
                if (row.relationship_role or "").strip().lower().replace(" ", "_") != "owner"
            ]
        self.result = _FakeRowsResult(rows)
        self.result.statement = statement
        return self.result


@pytest.mark.asyncio
async def test_get_deal_team_members_batch_keeps_unroled_user_participants() -> None:
    """Unroled user participants remain visible as ordinary deal team members."""
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


@pytest.mark.asyncio
async def test_get_deal_team_members_batch_excludes_normalized_owner_roles() -> None:
    """Owner projections are hidden from deal_team even when legacy rows use odd casing."""
    organisation_id = uuid4()
    deal_id = uuid4()
    collaborator_participant_id = uuid4()
    collaborator_user_id = uuid4()
    session = _CapturingSession(
        [
            SimpleNamespace(
                participant_id=uuid4(),
                deal_id=deal_id,
                related_local_id=uuid4(),
                related_name="Owner One",
                related_email="owner.one@example.com",
                relationship_role="owner",
            ),
            SimpleNamespace(
                participant_id=uuid4(),
                deal_id=deal_id,
                related_local_id=uuid4(),
                related_name="Owner Two",
                related_email="owner.two@example.com",
                relationship_role=" Owner ",
            ),
            SimpleNamespace(
                participant_id=collaborator_participant_id,
                deal_id=deal_id,
                related_local_id=collaborator_user_id,
                related_name="Avery Seller",
                related_email="avery@example.com",
                relationship_role="collaborator",
            ),
        ]
    )
    service = DealsAccessService(session, organisation_id)  # type: ignore[arg-type]

    result = await service.get_deal_team_members_batch([deal_id])

    compiled = str(session.result.statement).lower()
    assert "lower" in compiled
    assert "trim" in compiled
    assert "replace" in compiled
    assert result == {
        deal_id: [
            {
                "participant_id": collaborator_participant_id,
                "user_id": collaborator_user_id,
                "name": "Avery Seller",
                "email": "avery@example.com",
                "role": "collaborator",
            }
        ]
    }
