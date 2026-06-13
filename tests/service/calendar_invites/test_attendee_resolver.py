"""Tests for calendar invite attendee resolution guardrails."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from src.domain.models.master_tables.super_admin_models import AdminUserStatus
from src.domain.models.master_tables.user_models import SupabaseUserStatus, User, UserAppRole
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import ApprovalStatus
from src.service.calendar_invites.attendee_resolver import (
    AttendeeCandidate,
    CalendarInviteAttendeeResolver,
)

ORG_ID = UUID("11111111-1111-1111-1111-111111111111")
DEAL_ID = UUID("33333333-3333-3333-3333-333333333333")


@pytest.mark.asyncio
async def test_resolver_accepts_direct_email_without_confirmation() -> None:
    """Explicit attendee emails are safe to pass directly to the send service."""
    resolver = CalendarInviteAttendeeResolver(MagicMock(), organisation_id=ORG_ID)

    result = await resolver.resolve(["buyer@example.com"])

    assert not result.requires_confirmation
    assert not result.ambiguous
    assert result.resolved[0].email == "buyer@example.com"
    assert result.resolved[0].source == "direct_email"


@pytest.mark.asyncio
async def test_resolver_requires_confirmation_for_name_match(monkeypatch) -> None:
    """A single name match is still inferred identity and needs confirmation."""
    resolver = CalendarInviteAttendeeResolver(MagicMock(), organisation_id=ORG_ID)
    resolver._search_people = AsyncMock(  # noqa: SLF001
        return_value=[
            AttendeeCandidate(
                email="saksham@example.com",
                name="Saksham Bhutani",
                source="contact",
                source_id="contact-1",
                confidence="exact_name",
                input_value="Saksham Bhutani",
            )
        ]
    )

    result = await resolver.resolve(["Saksham Bhutani"])

    assert result.requires_confirmation
    assert result.confirmation_reasons == ["attendee_resolved_from_name"]
    assert result.resolved[0].email == "saksham@example.com"


@pytest.mark.asyncio
async def test_resolver_marks_multiple_name_matches_ambiguous() -> None:
    """Multiple possible people should block the invite until the user chooses."""
    resolver = CalendarInviteAttendeeResolver(MagicMock(), organisation_id=ORG_ID)
    resolver._search_people = AsyncMock(  # noqa: SLF001
        return_value=[
            AttendeeCandidate(
                email="saksham.one@example.com",
                name="Saksham One",
                source="contact",
                source_id="contact-1",
                confidence="partial_name",
                input_value="Saksham",
            ),
            AttendeeCandidate(
                email="saksham.two@example.com",
                name="Saksham Two",
                source="internal_user",
                source_id="user-1",
                confidence="partial_name",
                input_value="Saksham",
            ),
        ]
    )

    result = await resolver.resolve(["Saksham"])

    assert result.requires_confirmation
    assert result.ambiguous[0]["input"] == "Saksham"
    assert len(result.ambiguous[0]["options"]) == 2
    assert not result.resolved


@pytest.mark.asyncio
async def test_resolver_searches_active_approved_internal_users() -> None:
    """Named attendees can resolve to internal org users for calendar invites."""
    session = MagicMock()
    db_result = MagicMock()
    db_result.scalars.return_value.all.return_value = [
        User(
            organisation_id=ORG_ID,
            first_name="Saksham",
            last_name="Bhutani",
            email="saksham@copilotgtm.com",
            user_role=UserAppRole.MEMBER,
            organisation_role="Sales",
            status=SupabaseUserStatus.ACTIVE,
            admin_status=AdminUserStatus.APPROVED,
        )
    ]
    session.execute = AsyncMock(return_value=db_result)
    resolver = CalendarInviteAttendeeResolver(session, organisation_id=ORG_ID)

    result = await resolver._search_internal_users("Saksham Bhutani")  # noqa: SLF001

    assert result[0].email == "saksham@copilotgtm.com"
    assert result[0].name == "Saksham Bhutani"
    assert result[0].source == "internal_user"
    assert result[0].confidence == "exact_name"


@pytest.mark.asyncio
async def test_resolver_expands_deal_stakeholders_with_confirmation() -> None:
    """Deal-wide stakeholder expansion should always be confirmed before send."""
    resolver = CalendarInviteAttendeeResolver(MagicMock(), organisation_id=ORG_ID)
    resolver._resolve_deal_stakeholders = AsyncMock(  # noqa: SLF001
        return_value=(
            [
                AttendeeCandidate(
                    email="buyer@example.com",
                    name="Buyer One",
                    source="deal_stakeholder",
                    source_id="stakeholder-1",
                    confidence="deal_group",
                    input_value="all stakeholders",
                )
            ],
            [{"input": "Buyer Two", "source": "deal_stakeholder", "reason": "missing_email"}],
        )
    )

    result = await resolver.resolve(["all stakeholders"], deal_id=str(DEAL_ID))

    assert result.requires_confirmation
    assert "deal_stakeholder_group" in result.confirmation_reasons
    assert result.resolved[0].email == "buyer@example.com"
    assert result.missing[0]["reason"] == "missing_email"


@pytest.mark.asyncio
async def test_resolver_accepts_structured_deal_stakeholder_intent() -> None:
    """Agents can request deal stakeholder expansion with a structured payload."""
    resolver = CalendarInviteAttendeeResolver(MagicMock(), organisation_id=ORG_ID)
    resolver._resolve_deal_stakeholders = AsyncMock(  # noqa: SLF001
        return_value=(
            [
                AttendeeCandidate(
                    email="buyer@example.com",
                    name="Buyer One",
                    source="deal_stakeholder",
                    source_id="stakeholder-1",
                    confidence="deal_group",
                    input_value="all stakeholders",
                )
            ],
            [],
        )
    )

    result = await resolver.resolve([{"type": "deal_stakeholders", "deal_id": str(DEAL_ID)}])

    assert result.requires_confirmation
    assert result.resolved[0].email == "buyer@example.com"


@pytest.mark.asyncio
async def test_resolver_expands_deal_stakeholders_from_contact_participants() -> None:
    """Deal stakeholder expansion uses participant IDs as source IDs."""
    participant = CRMDealParticipant(
        organisation_id=ORG_ID,
        integration_id=uuid4(),
        deal_id=DEAL_ID,
        participant_type=CRMParticipantType.CONTACT,
        related_name="Buyer One",
        related_email="buyer@example.com",
        approval_status=ApprovalStatus.APPROVED.value,
    )

    class FakeSession:
        async def execute(self, statement):  # noqa: ANN001
            self.statement = statement
            return MagicMock(scalars=lambda: MagicMock(all=lambda: [participant]))

    session = FakeSession()
    resolver = CalendarInviteAttendeeResolver(session, organisation_id=ORG_ID)

    candidates, missing = await resolver._resolve_deal_stakeholders(DEAL_ID)  # noqa: SLF001

    assert missing == []
    assert candidates[0].email == "buyer@example.com"
    assert candidates[0].name == "Buyer One"
    assert candidates[0].source == "deal_stakeholder"
    assert candidates[0].source_id == str(participant.participant_id)
    compiled = str(session.statement)
    assert "crm_deal_participants" in compiled
    assert "deal_stakeholders" not in compiled
