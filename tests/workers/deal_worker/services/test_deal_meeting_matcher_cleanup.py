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
        """Return all scripted association rows for matcher cleanup scans."""
        return self._rows


class _FakeExecuteResult:
    def __init__(self, rows: list[SimpleNamespace]):
        self._rows = rows

    def scalars(self) -> _FakeScalarResult:
        """Expose the fake scalar-result API used by matcher queries."""
        return _FakeScalarResult(self._rows)


class _FakeSession:
    def __init__(self, rows: list[SimpleNamespace]):
        self._rows = rows

    async def execute(self, _stmt):  # noqa: ANN001
        """Return the same scripted rows for every cleanup query in this file."""
        return _FakeExecuteResult(self._rows)


# ---------------------------------------------------------------------------
# _cleanup_orphaned_associations
# ---------------------------------------------------------------------------


class TestCleanupOrphanedAssociations:
    """Contracts for hiding meeting cards when active associations become invalid."""

    @pytest.mark.asyncio
    async def test_deactivates_orphans(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Association whose event has NO current contact attendees gets REMOVED."""
        matcher = DealMeetingMatcher(uuid4())
        deal = SimpleNamespace(deal_id=uuid4())
        contact_emails = {"buyer@acme.com"}
        deleted_activity_ids = []

        orphan = SimpleNamespace(
            association_id=uuid4(),
            event_id=uuid4(),
            deal_id=deal.deal_id,
            status=AssociationStatus.ACTIVE,
            event=SimpleNamespace(
                organiser_email="saksham@copilotgtm.com",
                attendees=[SimpleNamespace(email="raman@copilotgtm.com")],
            ),
        )
        session = _FakeSession([orphan])

        class _ProjectionService:
            def __init__(self, projected_session, projected_org_id):  # noqa: ANN001
                assert projected_session is session
                assert projected_org_id == matcher.organisation_id

            async def soft_delete_activity_projection(self, activity_id):  # noqa: ANN001
                """Record the projected activity hidden by orphan cleanup."""
                deleted_activity_ids.append(activity_id)

        monkeypatch.setattr(
            "src.workers.deal_worker.services.deal_mapper_services.deal_meeting_matcher."
            "ActivityFeedProjectionService",
            _ProjectionService,
        )

        count = await matcher._cleanup_orphaned_associations(  # noqa: SLF001
            session, deal, contact_emails
        )

        assert count == 1
        assert orphan.status == AssociationStatus.REMOVED
        assert deleted_activity_ids == [orphan.association_id]

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
            association_id=uuid4(),
            event_id=uuid4(),
            deal_id=deal.deal_id,
            status=AssociationStatus.ACTIVE,
            event=SimpleNamespace(
                organiser_email="team@copilotgtm.com",
                attendees=[SimpleNamespace(email="other@copilotgtm.com")],
            ),
        )
        valid = SimpleNamespace(
            association_id=uuid4(),
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
        """Cleanup is a no-op when no active associations are returned."""
        matcher = DealMeetingMatcher(uuid4())
        deal = SimpleNamespace(deal_id=uuid4())

        session = _FakeSession([])

        count = await matcher._cleanup_orphaned_associations(  # noqa: SLF001
            session, deal, {"buyer@acme.com"}
        )

        assert count == 0


class TestCancelledAssociations:
    """Contracts for hiding projected activities when provider events are cancelled."""

    @pytest.mark.asyncio
    async def test_cancelled_event_associations_soft_delete_activity_projection(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cancelled meeting associations must stop showing in the deal feed."""
        matcher = DealMeetingMatcher(uuid4())
        association = SimpleNamespace(
            association_id=uuid4(),
            event_id=uuid4(),
            deal_id=uuid4(),
            status=AssociationStatus.ACTIVE,
        )
        session = _FakeSession([association])
        deleted_activity_ids = []

        class _ProjectionService:
            def __init__(self, projected_session, projected_org_id):  # noqa: ANN001
                assert projected_session is session
                assert projected_org_id == matcher.organisation_id

            async def soft_delete_activity_projection(self, activity_id):  # noqa: ANN001
                """Record the projected activity hidden by cancellation cleanup."""
                deleted_activity_ids.append(activity_id)

        monkeypatch.setattr(
            "src.workers.deal_worker.services.deal_mapper_services.deal_meeting_matcher."
            "ActivityFeedProjectionService",
            _ProjectionService,
        )

        count = await matcher._handle_cancelled_associations(session)  # noqa: SLF001

        assert count == 1
        assert association.status == AssociationStatus.CANCELLED
        assert deleted_activity_ids == [association.association_id]
