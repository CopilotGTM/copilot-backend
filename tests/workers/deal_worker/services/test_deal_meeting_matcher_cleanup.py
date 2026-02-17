"""Tests for orphaned association cleanup in DealMeetingMatcher."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.calendar import AssociationStatus
from src.workers.deal_worker.services.deal_mapper_services.deal_meeting_matcher import (
    DealMeetingMatcher,
)

# ---------------------------------------------------------------------------
# Fake session helpers (same pattern as test_stakeholder_last_contact_updates)
# ---------------------------------------------------------------------------


class _FakeScalarResult:
    def __init__(self, rows: list[SimpleNamespace]):
        self._rows = rows

    def all(self) -> list[SimpleNamespace]:
        return self._rows


class _FakeExecuteResult:
    def __init__(self, rows: list[SimpleNamespace]):
        self._rows = rows

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._rows)


class _FakeSession:
    def __init__(self, rows: list[SimpleNamespace]):
        self._rows = rows

    async def execute(self, _stmt):  # noqa: ANN001
        return _FakeExecuteResult(self._rows)


# ---------------------------------------------------------------------------
# _cleanup_orphaned_associations
# ---------------------------------------------------------------------------


class TestCleanupOrphanedAssociations:
    @pytest.mark.asyncio
    async def test_deactivates_orphans(self) -> None:
        """Association whose event has NO current contact attendees gets REMOVED."""
        matcher = DealMeetingMatcher(uuid4())
        deal = SimpleNamespace(deal_id=uuid4())
        contact_emails = {"buyer@acme.com"}

        orphan = SimpleNamespace(
            event_id=uuid4(),
            deal_id=deal.deal_id,
            status=AssociationStatus.ACTIVE,
            event=SimpleNamespace(
                organiser_email="saksham@copilotgtm.com",
                attendees=[SimpleNamespace(email="raman@copilotgtm.com")],
            ),
        )
        session = _FakeSession([orphan])

        count = await matcher._cleanup_orphaned_associations(  # noqa: SLF001
            session, deal, contact_emails
        )

        assert count == 1
        assert orphan.status == AssociationStatus.REMOVED

    @pytest.mark.asyncio
    async def test_preserves_valid_associations(self) -> None:
        """Association whose event has a matching contact attendee stays ACTIVE."""
        matcher = DealMeetingMatcher(uuid4())
        deal = SimpleNamespace(deal_id=uuid4())
        contact_emails = {"buyer@acme.com"}

        valid = SimpleNamespace(
            event_id=uuid4(),
            deal_id=deal.deal_id,
            status=AssociationStatus.ACTIVE,
            event=SimpleNamespace(
                organiser_email="owner@ourco.com",
                attendees=[
                    SimpleNamespace(email="buyer@acme.com"),
                    SimpleNamespace(email="owner@ourco.com"),
                ],
            ),
        )
        session = _FakeSession([valid])

        count = await matcher._cleanup_orphaned_associations(  # noqa: SLF001
            session, deal, contact_emails
        )

        assert count == 0
        assert valid.status == AssociationStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_mixed_orphan_and_valid(self) -> None:
        """Only orphaned associations are deactivated; valid ones survive."""
        matcher = DealMeetingMatcher(uuid4())
        deal = SimpleNamespace(deal_id=uuid4())
        contact_emails = {"buyer@acme.com", "cfo@acme.com"}

        orphan = SimpleNamespace(
            event_id=uuid4(),
            deal_id=deal.deal_id,
            status=AssociationStatus.ACTIVE,
            event=SimpleNamespace(
                organiser_email="team@copilotgtm.com",
                attendees=[SimpleNamespace(email="other@copilotgtm.com")],
            ),
        )
        valid = SimpleNamespace(
            event_id=uuid4(),
            deal_id=deal.deal_id,
            status=AssociationStatus.ACTIVE,
            event=SimpleNamespace(
                organiser_email="owner@ourco.com",
                attendees=[SimpleNamespace(email="cfo@acme.com")],
            ),
        )
        session = _FakeSession([orphan, valid])

        count = await matcher._cleanup_orphaned_associations(  # noqa: SLF001
            session, deal, contact_emails
        )

        assert count == 1
        assert orphan.status == AssociationStatus.REMOVED
        assert valid.status == AssociationStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_contact_as_organizer_preserves_association(self) -> None:
        """If the contact is the organizer (not attendee), the association is still valid."""
        matcher = DealMeetingMatcher(uuid4())
        deal = SimpleNamespace(deal_id=uuid4())
        contact_emails = {"buyer@acme.com"}

        assoc = SimpleNamespace(
            event_id=uuid4(),
            deal_id=deal.deal_id,
            status=AssociationStatus.ACTIVE,
            event=SimpleNamespace(
                organiser_email="buyer@acme.com",
                attendees=[SimpleNamespace(email="owner@ourco.com")],
            ),
        )
        session = _FakeSession([assoc])

        count = await matcher._cleanup_orphaned_associations(  # noqa: SLF001
            session, deal, contact_emails
        )

        assert count == 0
        assert assoc.status == AssociationStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_no_associations_returns_zero(self) -> None:
        matcher = DealMeetingMatcher(uuid4())
        deal = SimpleNamespace(deal_id=uuid4())

        session = _FakeSession([])

        count = await matcher._cleanup_orphaned_associations(  # noqa: SLF001
            session, deal, {"buyer@acme.com"}
        )

        assert count == 0
