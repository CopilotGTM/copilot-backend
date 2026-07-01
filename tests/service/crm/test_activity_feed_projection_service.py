"""Regression tests for activity-feed projection rows and object links.

The service is tested with captured SQL statements so projection payloads,
source identities, and link roles stay stable without needing a live database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.sql.dml import Delete
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarAttendee,
    CalendarEvent,
    CalendarEventDealAssociation,
)
from src.domain.models.organisation_tables.sync.crm import CRMContact, CRMDeal, CRMNote, CRMTask
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMDealStageHistory,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.email_models import (
    DealEmailAssociation,
    DealEmailDirection,
    Email,
    EmailDirection,
)
from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    DealRiskBlocker,
    RiskBlockerCategory,
    RiskBlockerSeverity,
    RiskBlockerSource,
    RiskBlockerStatus,
    RiskBlockerType,
)
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService


class _FakeScalarResult:
    def __init__(self, rows: list[object]):
        self._rows = rows

    def all(self) -> list[object]:
        """Return rows in the same shape SQLAlchemy scalar results expose."""
        return self._rows

    def first(self) -> object | None:
        """Support older projection tests that inspect the first scalar row."""
        return self._rows[0] if self._rows else None


class _FakeExecuteResult:
    def __init__(self, rows: list[object] | None = None):
        self._rows = rows or []

    def scalars(self) -> _FakeScalarResult:
        """Expose fake ORM rows for service lookups inside projection methods."""
        return _FakeScalarResult(self._rows)

    def scalar_one_or_none(self) -> object | None:
        """Match the deal lookup contract used by company-link resolution."""
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(
        self,
        *,
        deal: object | None = None,
        contacts: list[object] | None = None,
    ) -> None:
        self.statements: list[object] = []
        self._deal = deal
        self._contacts = contacts or []

    async def execute(self, statement):  # noqa: ANN001
        """Capture writes and return scripted rows for org-scoped local lookups."""
        self.statements.append(statement)
        statement_text = str(statement)
        if "FROM crm_deals" in statement_text:
            return _FakeExecuteResult([self._deal] if self._deal else [])
        if "FROM crm_contacts" in statement_text:
            return _FakeExecuteResult(self._contacts)
        return _FakeExecuteResult()


def _compiled_values(statement: object) -> dict[str, object]:
    return statement.compile().params


def _activity_link_values(session: _FakeSession) -> set[tuple[object, object, object, object]]:
    link_values = [
        _compiled_values(statement)
        for statement in session.statements
        if "crm_activity_links" in str(statement) and not isinstance(statement, Delete)
    ]
    return {
        (values["object_type"], values["object_id"], values["link_role"], values["link_source"])
        for values in link_values
    }


def _activity_values_by_source_type(session: _FakeSession, source_type: str) -> dict[str, object]:
    for statement in session.statements:
        values = _compiled_values(statement)
        if values.get("source_type") == source_type:
            return values
    raise AssertionError(f"activity source_type not found: {source_type}")


@pytest.mark.asyncio
async def test_contact_created_projection_writes_contact_activity_and_links() -> None:
    """Manual contact creation should be visible on the contact and account activity feeds."""
    organisation_id = uuid4()
    integration_id = uuid4()
    contact_id = uuid4()
    company_id = uuid4()
    created_at = datetime(2026, 6, 25, 9, 0, tzinfo=UTC)
    contact = CRMContact(
        contact_id=contact_id,
        organisation_id=organisation_id,
        integration_id=integration_id,
        full_name="Jane Buyer",
        email="jane@example.com",
        job_title="CFO",
        company_id=company_id,
        company_name="Acme",
        created_at=created_at,
    )
    session = _FakeSession()

    await ActivityFeedProjectionService(session, organisation_id).upsert_contact_created(contact)

    values = _compiled_values(session.statements[0])
    assert values["activity_id"] == contact_id
    assert values["activity_type"] == "EVENT"
    assert values["title"] == "Contact created: Jane Buyer"
    assert values["summary"] == "jane@example.com"
    assert values["preview"] == "jane@example.com"
    assert values["occurred_at"] == created_at
    assert values["source_type"] == "CONTACT_CREATED"
    assert values["source_record_id"] == str(contact_id)
    assert values["source_integration_id"] == integration_id
    assert values["display_payload"] == {
        "contact_name": "Jane Buyer",
        "email": "jane@example.com",
        "job_title": "CFO",
        "company_name": "Acme",
    }
    assert _activity_link_values(session) == {
        ("CONTACT", contact_id, "RELATED", "SYSTEM"),
        ("COMPANY", company_id, "RELATED", "SYSTEM"),
    }


@pytest.mark.asyncio
async def test_stakeholder_added_projection_writes_deal_and_contact_activity_links() -> None:
    """Adding a stakeholder should appear in the activity feed, not legacy timeline events."""
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    participant_id = uuid4()
    contact_id = uuid4()
    actor_user_id = uuid4()
    occurred_at = datetime(2026, 6, 25, 10, 0, tzinfo=UTC)
    participant = CRMDealParticipant(
        participant_id=participant_id,
        organisation_id=organisation_id,
        integration_id=integration_id,
        deal_id=deal_id,
        participant_type=CRMParticipantType.CONTACT,
        related_local_id=contact_id,
        related_name="Jane Buyer",
        related_email="jane@example.com",
        job_title="CFO",
        participant_source="MANUAL",
        created_at=occurred_at,
    )
    session = _FakeSession()

    await ActivityFeedProjectionService(session, organisation_id).upsert_stakeholder_added(
        participant,
        actor_user_id=actor_user_id,
        source="agent",
        created_via="add_stakeholder_agent",
    )

    values = _compiled_values(session.statements[0])
    assert values["activity_id"] == participant_id
    assert values["activity_type"] == "EVENT"
    assert values["title"] == "Stakeholder added: Jane Buyer"
    assert values["summary"] == "jane@example.com"
    assert values["preview"] == "jane@example.com"
    assert values["occurred_at"] == occurred_at
    assert values["source_type"] == "STAKEHOLDER_ADDED"
    assert values["source_record_id"] == str(participant_id)
    assert values["source_integration_id"] == integration_id
    assert values["display_payload"] == {
        "participant_id": str(participant_id),
        "contact_id": str(contact_id),
        "stakeholder_name": "Jane Buyer",
        "email": "jane@example.com",
        "job_title": "CFO",
        "participant_source": "MANUAL",
        "mutation_source": "agent",
        "created_via": "add_stakeholder_agent",
    }
    assert _activity_link_values(session) == {
        ("DEAL", deal_id, "PRIMARY", "SYSTEM"),
        ("CONTACT", contact_id, "RELATED", "SYSTEM"),
        ("USER", actor_user_id, "OWNER", "SYSTEM"),
    }


@pytest.mark.asyncio
async def test_stakeholder_removed_projection_writes_distinct_activity() -> None:
    """Removing a stakeholder should use a separate activity from the add card."""
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    participant_id = uuid4()
    contact_id = uuid4()
    actor_user_id = uuid4()
    removed_at = datetime(2026, 6, 25, 11, 0, tzinfo=UTC)
    participant = CRMDealParticipant(
        participant_id=participant_id,
        organisation_id=organisation_id,
        integration_id=integration_id,
        deal_id=deal_id,
        participant_type=CRMParticipantType.CONTACT,
        related_local_id=contact_id,
        related_name="Jane Buyer",
        related_email="jane@example.com",
        job_title="CFO",
        participant_source="MANUAL",
        updated_at=removed_at,
    )
    session = _FakeSession()

    await ActivityFeedProjectionService(session, organisation_id).upsert_stakeholder_removed(
        participant,
        actor_user_id=actor_user_id,
        source="api",
        created_via="remove_stakeholder",
    )

    values = _compiled_values(session.statements[0])
    assert values["activity_id"] != participant_id
    assert values["activity_type"] == "EVENT"
    assert values["title"] == "Stakeholder removed: Jane Buyer"
    assert values["occurred_at"] == removed_at
    assert values["source_type"] == "STAKEHOLDER_REMOVED"
    assert values["source_record_id"] == f"{participant_id}:STAKEHOLDER_REMOVED"
    assert values["display_payload"]["participant_id"] == str(participant_id)
    assert values["display_payload"]["mutation_source"] == "api"
    assert values["display_payload"]["created_via"] == "remove_stakeholder"
    assert _activity_link_values(session) == {
        ("DEAL", deal_id, "PRIMARY", "SYSTEM"),
        ("CONTACT", contact_id, "RELATED", "SYSTEM"),
        ("USER", actor_user_id, "OWNER", "SYSTEM"),
    }


@pytest.mark.asyncio
async def test_contact_enrichment_lifecycle_projection_writes_deterministic_events() -> None:
    """Enrichment trigger and completion cards should be replay-safe per job."""
    organisation_id = uuid4()
    contact_id = uuid4()
    company_id = uuid4()
    user_id = uuid4()
    job_id = uuid4()
    occurred_at = datetime(2026, 6, 25, 9, 30, tzinfo=UTC)
    contact = CRMContact(
        contact_id=contact_id,
        organisation_id=organisation_id,
        integration_id=uuid4(),
        full_name="Jane Buyer",
        email="jane@example.com",
        company_id=company_id,
        profile_enrichment_source="apollo",
    )
    session = _FakeSession()

    service = ActivityFeedProjectionService(session, organisation_id)
    await service.upsert_contact_enrichment_triggered(
        contact,
        job_id=job_id,
        actor_user_id=user_id,
        occurred_at=occurred_at,
    )
    await service.upsert_contact_enrichment_completed(
        contact,
        job_id=job_id,
        provider_status="found",
        summary_status="generated",
        occurred_at=occurred_at,
    )

    triggered = _activity_values_by_source_type(session, "CONTACT_ENRICHMENT_TRIGGERED")
    completed = _activity_values_by_source_type(session, "CONTACT_ENRICHMENT_COMPLETED")
    assert triggered["activity_id"] != completed["activity_id"]
    assert triggered["source_type"] == "CONTACT_ENRICHMENT_TRIGGERED"
    assert triggered["source_record_id"] == f"{contact_id}:CONTACT_ENRICHMENT_TRIGGERED:{job_id}"
    assert triggered["title"] == "Contact enrichment triggered"
    assert triggered["display_payload"] == {
        "contact_name": "Jane Buyer",
        "email": "jane@example.com",
        "job_id": str(job_id),
    }
    assert completed["source_type"] == "CONTACT_ENRICHMENT_COMPLETED"
    assert completed["source_record_id"] == f"{contact_id}:CONTACT_ENRICHMENT_COMPLETED:{job_id}"
    assert completed["title"] == "Contact enrichment completed"
    assert completed["display_payload"] == {
        "contact_name": "Jane Buyer",
        "email": "jane@example.com",
        "job_id": str(job_id),
        "provider_status": "found",
        "summary_status": "generated",
        "provider": "apollo",
    }
    assert _activity_link_values(session) == {
        ("CONTACT", contact_id, "RELATED", "SYSTEM"),
        ("COMPANY", company_id, "RELATED", "SYSTEM"),
        ("USER", user_id, "OWNER", "SYSTEM"),
    }


@pytest.mark.asyncio
async def test_task_projection_uses_source_id_payload_and_assignee_actor() -> None:
    """Task projection preserves source identity and resolved actor display fields."""
    organisation_id = uuid4()
    task_id = uuid4()
    integration_id = uuid4()
    contact_id = uuid4()
    company_id = uuid4()
    task = CRMTask(
        task_id=task_id,
        organisation_id=organisation_id,
        integration_id=integration_id,
        title="Follow up",
        description="Call buyer",
        status="IN_PROGRESS",
        priority="HIGH",
        completed_at=datetime(2026, 6, 22, 10, 30, tzinfo=UTC),
        assigned_to_name="Alex Morgan",
        assigned_to_email="alex@example.com",
        task_metadata={
            "provider": "salesforce",
            "task_type": "TASK",
            "contact_id": str(contact_id),
            "company_id": str(company_id),
        },
    )
    session = _FakeSession()

    await ActivityFeedProjectionService(session, organisation_id).upsert_task(task)

    values = _compiled_values(session.statements[0])
    assert values["activity_id"] == task_id
    assert values["source_record_id"] == str(task_id)
    assert values["source_provider"] == "salesforce"
    assert values["actor_name"] == "Alex Morgan"
    assert values["actor_email"] == "alex@example.com"
    assert values["display_payload"] == {
        "status": "IN_PROGRESS",
        "priority": "HIGH",
        "task_type": "TASK",
        "completed_at": "2026-06-22T10:30:00+00:00",
    }

    assert _activity_link_values(session) == {
        ("CONTACT", contact_id, "RELATED", "SYSTEM"),
        ("COMPANY", company_id, "RELATED", "SYSTEM"),
    }


@pytest.mark.asyncio
async def test_task_projection_removes_stale_contact_and_company_links() -> None:
    """Task replay deletes optional links when metadata no longer resolves IDs."""
    organisation_id = uuid4()
    task_id = uuid4()
    task = CRMTask(
        task_id=task_id,
        organisation_id=organisation_id,
        integration_id=uuid4(),
        title="Follow up",
        task_metadata={"provider": "salesforce"},
    )
    session = _FakeSession()

    await ActivityFeedProjectionService(session, organisation_id).upsert_task(task)

    delete_params = [
        _compiled_values(statement)
        for statement in session.statements
        if isinstance(statement, Delete)
    ]
    assert any("CONTACT" in params.values() for params in delete_params)
    assert any("COMPANY" in params.values() for params in delete_params)


@pytest.mark.asyncio
async def test_task_projection_uses_explicit_actor_metadata() -> None:
    """Provider actor metadata wins over assignee fields when both are present."""
    organisation_id = uuid4()
    integration_id = uuid4()
    task = CRMTask(
        organisation_id=organisation_id,
        integration_id=integration_id,
        title="Follow up",
        assigned_to_name="Alex Morgan",
        assigned_to_email="alex@example.com",
        task_metadata={
            "actor_name": "Workflow Bot",
            "actor_email": "bot@example.com",
        },
    )
    session = _FakeSession()

    await ActivityFeedProjectionService(session, organisation_id).upsert_task(task)

    insert_values = _compiled_values(session.statements[0])
    assert insert_values["actor_name"] == "Workflow Bot"
    assert insert_values["actor_email"] == "bot@example.com"


@pytest.mark.asyncio
async def test_note_projection_uses_source_id_payload_provider_and_related_links() -> None:
    """Note projection keeps note source identity and materializes metadata links."""
    organisation_id = uuid4()
    note_id = uuid4()
    integration_id = uuid4()
    contact_id = uuid4()
    company_id = uuid4()
    note = CRMNote(
        note_id=note_id,
        organisation_id=organisation_id,
        integration_id=integration_id,
        title="Buyer note",
        body="Spoke with buyer offline",
        note_metadata={
            "provider": "salesforce",
            "contact_id": str(contact_id),
            "company_id": str(company_id),
        },
    )
    session = _FakeSession()

    await ActivityFeedProjectionService(session, organisation_id).upsert_note(note)

    values = _compiled_values(session.statements[0])
    assert values["source_record_id"] == str(note_id)
    assert values["source_provider"] == "salesforce"
    assert values["display_payload"] == {
        "body_format": "plain_text",
        "visibility": "TEAM",
        "has_attachments": False,
    }

    assert _activity_link_values(session) == {
        ("CONTACT", contact_id, "RELATED", "SYSTEM"),
        ("COMPANY", company_id, "RELATED", "SYSTEM"),
    }


@pytest.mark.asyncio
async def test_email_association_projection_writes_email_activity_and_safe_links() -> None:
    """Email associations create one card with only org-resolved contact/company links."""
    organisation_id = uuid4()
    deal_id = uuid4()
    association_id = uuid4()
    contact_id = uuid4()
    company_id = uuid4()
    received_at = datetime(2026, 6, 22, 14, 15, tzinfo=UTC)
    association = DealEmailAssociation(
        association_id=association_id,
        organisation_id=organisation_id,
        deal_id=deal_id,
        internet_message_id="<message-1@example.com>",
        status=AssociationStatus.ACTIVE,
        direction=DealEmailDirection.INBOUND,
        matched_stakeholder_emails=["Buyer@Example.com"],
        email_subject="Pricing follow-up",
        email_date=received_at,
    )
    email = Email(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        folder_id=uuid4(),
        external_message_id="provider-message-1",
        internet_message_id="<message-1@example.com>",
        from_email="buyer@example.com",
        from_name="Buyer",
        to_emails=["seller@ourco.com"],
        cc_emails=["finance@example.com"],
        subject="Pricing follow-up",
        snippet="Can you send the new quote?",
        received_at=received_at,
        sent_at=received_at,
        direction=EmailDirection.INBOUND,
        has_attachments=True,
        email_metadata={"provider": "gmail"},
    )
    session = _FakeSession(
        deal=SimpleNamespace(company_id=company_id),
        contacts=[SimpleNamespace(contact_id=contact_id, company_id=company_id)],
    )

    await ActivityFeedProjectionService(session, organisation_id).upsert_email_association(
        association,
        email,
    )

    values = _compiled_values(session.statements[2])
    assert values["activity_id"] == association_id
    assert values["activity_type"] == "EMAIL"
    assert values["title"] == "Pricing follow-up"
    assert values["preview"] == "Can you send the new quote?"
    assert values["occurred_at"] == received_at
    assert values["source_type"] == "EMAIL"
    assert values["source_record_id"] == str(association_id)
    assert values["source_provider"] == "gmail"
    assert values["actor_name"] == "Buyer"
    assert values["actor_email"] == "buyer@example.com"
    assert values["display_payload"] == {
        "direction": "INBOUND",
        "participants_preview": [
            {"name": "Buyer", "email": "buyer@example.com", "role": "sender"},
            {"email": "seller@ourco.com", "role": "recipient"},
            {"email": "finance@example.com", "role": "recipient"},
        ],
        "participant_count": 3,
        "has_attachments": True,
    }
    assert _activity_link_values(session) == {
        ("DEAL", deal_id, "PRIMARY", "SYSTEM"),
        ("CONTACT", contact_id, "RELATED", "SYSTEM"),
        ("COMPANY", company_id, "RELATED", "SYSTEM"),
    }


@pytest.mark.asyncio
async def test_meeting_association_projection_writes_meeting_activity_and_attendee_links() -> None:
    """Meeting associations create one card with attendee contact links and deal company links."""
    organisation_id = uuid4()
    deal_id = uuid4()
    association_id = uuid4()
    contact_id = uuid4()
    company_id = uuid4()
    start_at = datetime(2026, 6, 22, 15, 0, tzinfo=UTC)
    end_at = datetime(2026, 6, 22, 15, 45, tzinfo=UTC)
    association = CalendarEventDealAssociation(
        association_id=association_id,
        organisation_id=organisation_id,
        deal_id=deal_id,
        event_id=uuid4(),
        status=AssociationStatus.ACTIVE,
    )
    event = CalendarEvent(
        organisation_id=organisation_id,
        event_id=association.event_id,
        external_id="calendar-event-1",
        provider="google",
        title="Renewal review",
        start_at=start_at,
        end_at=end_at,
        status="confirmed",
        organiser_email="seller@ourco.com",
        organiser_name="Seller",
        attendee_count=2,
    )
    event.attendees = [
        CalendarAttendee(
            event_id=event.event_id,
            email="buyer@example.com",
            display_name="Buyer",
            response_status="accepted",
        ),
        CalendarAttendee(
            event_id=event.event_id,
            email="seller@ourco.com",
            display_name="Seller",
            response_status="accepted",
            is_organiser=True,
        ),
    ]
    session = _FakeSession(
        deal=SimpleNamespace(company_id=company_id),
        contacts=[SimpleNamespace(contact_id=contact_id, company_id=company_id)],
    )

    await ActivityFeedProjectionService(session, organisation_id).upsert_meeting_association(
        association,
        event,
    )

    values = _compiled_values(session.statements[2])
    assert values["activity_id"] == association_id
    assert values["activity_type"] == "MEETING"
    assert values["title"] == "Renewal review"
    assert values["occurred_at"] == start_at
    assert values["source_type"] == "MEETING"
    assert values["source_record_id"] == str(association_id)
    assert values["source_provider"] == "google"
    assert values["actor_name"] == "Seller"
    assert values["actor_email"] == "seller@ourco.com"
    assert values["display_payload"] == {
        "participants_preview": [
            {"name": "Buyer", "email": "buyer@example.com", "role": "attendee"},
            {"name": "Seller", "email": "seller@ourco.com", "role": "organizer"},
        ],
        "participant_count": 2,
        "duration_minutes": 45,
        "meeting_status": "confirmed",
    }
    assert _activity_link_values(session) == {
        ("DEAL", deal_id, "PRIMARY", "SYSTEM"),
        ("CONTACT", contact_id, "ATTENDEE", "SYSTEM"),
        ("COMPANY", company_id, "RELATED", "SYSTEM"),
    }


@pytest.mark.asyncio
async def test_deal_created_projection_writes_event_activity_and_available_local_links() -> None:
    """Deal-created projection uses the deal row as source and only local IDs for links."""
    organisation_id = uuid4()
    deal_id = uuid4()
    integration_id = uuid4()
    company_id = uuid4()
    contact_id = uuid4()
    owner_id = uuid4()
    created_at = datetime(2026, 6, 22, 9, 15, tzinfo=UTC)
    deal = CRMDeal(
        deal_id=deal_id,
        organisation_id=organisation_id,
        integration_id=integration_id,
        name="Acme renewal",
        description="Renewal for Acme",
        source_created_at=created_at,
        company_id=company_id,
        primary_contact_id=contact_id,
        owner_id=owner_id,
        owner_name="Owner User",
        owner_email="owner@example.com",
    )
    session = _FakeSession()

    await ActivityFeedProjectionService(session, organisation_id).upsert_deal_created(deal)

    values = _compiled_values(session.statements[0])
    assert values["activity_id"] == deal_id
    assert values["activity_type"] == "EVENT"
    assert values["title"] == "Deal created: Acme renewal"
    assert values["summary"] == "Renewal for Acme"
    assert values["preview"] == "Renewal for Acme"
    assert values["occurred_at"] == created_at
    assert values["source_type"] == "DEAL_CREATED"
    assert values["source_record_id"] == str(deal_id)
    assert values["source_integration_id"] == integration_id
    assert values["actor_name"] == "Owner User"
    assert values["actor_email"] == "owner@example.com"
    assert values["display_payload"] == {"deal_name": "Acme renewal"}
    assert _activity_link_values(session) == {
        ("DEAL", deal_id, "PRIMARY", "SYSTEM"),
        ("COMPANY", company_id, "RELATED", "SYSTEM"),
        ("CONTACT", contact_id, "RELATED", "SYSTEM"),
        ("USER", owner_id, "OWNER", "SYSTEM"),
    }


@pytest.mark.asyncio
async def test_stage_change_projection_writes_event_activity_and_deal_owner_links() -> None:
    """Stage-history rows create one activity keyed by the history row."""
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    history_id = uuid4()
    company_id = uuid4()
    owner_id = uuid4()
    entered_at = datetime(2026, 6, 22, 11, 45, tzinfo=UTC)
    history = CRMDealStageHistory(
        history_id=history_id,
        organisation_id=organisation_id,
        integration_id=integration_id,
        deal_id=deal_id,
        stage_id=uuid4(),
        from_stage_id=uuid4(),
        entered_at=entered_at,
        source="api",
    )
    deal = CRMDeal(
        deal_id=deal_id,
        organisation_id=organisation_id,
        integration_id=integration_id,
        name="Acme renewal",
        stage_name="Proposal",
        company_id=company_id,
        owner_id=owner_id,
        owner_name="Owner User",
        owner_email="owner@example.com",
    )
    session = _FakeSession(deal=deal)

    await ActivityFeedProjectionService(session, organisation_id).upsert_stage_change(history)

    values = _compiled_values(session.statements[1])
    assert values["activity_id"] == history_id
    assert values["activity_type"] == "EVENT"
    assert values["title"] == "Stage changed: Proposal"
    assert values["occurred_at"] == entered_at
    assert values["source_type"] == "STAGE_CHANGE"
    assert values["source_record_id"] == str(history_id)
    assert values["source_provider"] == "api"
    assert values["display_payload"] == {
        "deal_name": "Acme renewal",
        "stage_name": "Proposal",
        "stage_id": str(history.stage_id),
        "from_stage_id": str(history.from_stage_id),
    }
    assert _activity_link_values(session) == {
        ("DEAL", deal_id, "PRIMARY", "SYSTEM"),
        ("COMPANY", company_id, "RELATED", "SYSTEM"),
        ("USER", owner_id, "OWNER", "SYSTEM"),
    }


@pytest.mark.asyncio
async def test_risk_blocker_projection_writes_opened_lifecycle_activity() -> None:
    """Risk and blocker opening events create compact deal-linked feed cards."""
    organisation_id = uuid4()
    deal_id = uuid4()
    risk_blocker_id = uuid4()
    identified_at = datetime(2026, 6, 23, 12, 30, tzinfo=UTC)
    item = DealRiskBlocker(
        risk_blocker_id=risk_blocker_id,
        organisation_id=organisation_id,
        deal_id=deal_id,
        item_type=RiskBlockerType.RISK,
        title="Security review may delay close",
        description="Security review has no committed date.",
        category=RiskBlockerCategory.SECURITY,
        severity=RiskBlockerSeverity.HIGH,
        status=RiskBlockerStatus.OPEN,
        source_type=RiskBlockerSource.MEETING_GOOGLE,
        source_record_id="meeting-123",
        is_ai_generated=True,
        owner_name="Alex Owner",
        owner_email="alex@example.com",
        identified_at=identified_at,
    )
    session = _FakeSession()

    await ActivityFeedProjectionService(session, organisation_id).upsert_risk_blocker_lifecycle(
        item,
        source_type="RISK_OPENED",
    )

    values = _compiled_values(session.statements[0])
    assert values["activity_id"] == risk_blocker_id
    assert values["activity_type"] == "EVENT"
    assert values["title"] == "Risk Opened: Security review may delay close"
    assert values["summary"] == "Security review has no committed date."
    assert values["preview"] == "Security review has no committed date."
    assert values["occurred_at"] == identified_at
    assert values["source_type"] == "RISK_OPENED"
    assert values["source_record_id"] == str(risk_blocker_id)
    assert values["actor_name"] == "Alex Owner"
    assert values["actor_email"] == "alex@example.com"
    assert values["display_payload"] == {
        "risk_blocker_id": str(risk_blocker_id),
        "item_type": "RISK",
        "severity": "HIGH",
        "category": "SECURITY",
        "status": "OPEN",
        "source_type": "MEETING_GOOGLE",
        "is_ai_generated": True,
        "owner_name": "Alex Owner",
        "owner_email": "alex@example.com",
    }
    assert _activity_link_values(session) == {("DEAL", deal_id, "PRIMARY", "SYSTEM")}


@pytest.mark.asyncio
async def test_risk_blocker_projection_writes_resolved_lifecycle_activity() -> None:
    """Resolved lifecycle rows preserve chronology with a separate deterministic card."""
    organisation_id = uuid4()
    deal_id = uuid4()
    risk_blocker_id = uuid4()
    closed_at = datetime(2026, 6, 23, 16, 45, tzinfo=UTC)
    item = DealRiskBlocker(
        risk_blocker_id=risk_blocker_id,
        organisation_id=organisation_id,
        deal_id=deal_id,
        item_type=RiskBlockerType.BLOCKER,
        title="Legal redlines pending",
        description="Customer legal has not approved the MSA.",
        category=RiskBlockerCategory.CONTRACT,
        severity=RiskBlockerSeverity.CRITICAL,
        status=RiskBlockerStatus.RESOLVED,
        source_type=RiskBlockerSource.MANUAL,
        is_ai_generated=False,
        owner_name="Legal Owner",
        owner_email="legal@example.com",
        resolution_notes="MSA approved by legal.",
        closed_at=closed_at,
        closed_by_user_name="Casey Closer",
    )
    session = _FakeSession()

    await ActivityFeedProjectionService(session, organisation_id).upsert_risk_blocker_lifecycle(
        item,
        source_type="BLOCKER_RESOLVED",
    )

    values = _compiled_values(session.statements[0])
    assert values["activity_id"] != risk_blocker_id
    assert values["activity_type"] == "EVENT"
    assert values["title"] == "Blocker Resolved: Legal redlines pending"
    assert values["occurred_at"] == closed_at
    assert values["source_type"] == "BLOCKER_RESOLVED"
    assert (
        values["source_record_id"] == f"{risk_blocker_id}:BLOCKER_RESOLVED:{closed_at.isoformat()}"
    )
    assert values["actor_name"] == "Casey Closer"
    assert values["display_payload"] == {
        "risk_blocker_id": str(risk_blocker_id),
        "item_type": "BLOCKER",
        "severity": "CRITICAL",
        "category": "CONTRACT",
        "status": "RESOLVED",
        "source_type": "MANUAL",
        "is_ai_generated": False,
        "owner_name": "Legal Owner",
        "owner_email": "legal@example.com",
        "resolution_notes": "MSA approved by legal.",
    }
    assert _activity_link_values(session) == {("DEAL", deal_id, "PRIMARY", "SYSTEM")}


@pytest.mark.asyncio
async def test_risk_projection_writes_risk_resolved_lifecycle_activity() -> None:
    """Risk closures should not be labeled as blocker lifecycle events."""
    organisation_id = uuid4()
    deal_id = uuid4()
    risk_blocker_id = uuid4()
    closed_at = datetime(2026, 6, 23, 16, 45, tzinfo=UTC)
    item = DealRiskBlocker(
        risk_blocker_id=risk_blocker_id,
        organisation_id=organisation_id,
        deal_id=deal_id,
        item_type=RiskBlockerType.RISK,
        title="Security review delay",
        description="Security review is done.",
        category=RiskBlockerCategory.SECURITY,
        severity=RiskBlockerSeverity.HIGH,
        status=RiskBlockerStatus.RESOLVED,
        source_type=RiskBlockerSource.MANUAL,
        resolution_notes="Security approved.",
        closed_at=closed_at,
    )
    session = _FakeSession()

    await ActivityFeedProjectionService(session, organisation_id).upsert_risk_blocker_lifecycle(
        item,
        source_type="RISK_RESOLVED",
    )

    values = _compiled_values(session.statements[0])
    assert values["title"] == "Risk Resolved: Security review delay"
    assert values["source_type"] == "RISK_RESOLVED"
    assert values["source_record_id"] == f"{risk_blocker_id}:RISK_RESOLVED:{closed_at.isoformat()}"
    assert values["display_payload"]["item_type"] == "RISK"
    assert values["display_payload"]["resolution_notes"] == "Security approved."


@pytest.mark.asyncio
async def test_risk_blocker_reopen_projection_uses_audit_timestamp_identity() -> None:
    """Reopen cards use the audit event timestamp so later edits do not change identity."""
    organisation_id = uuid4()
    deal_id = uuid4()
    risk_blocker_id = uuid4()
    audit_at = datetime(2026, 6, 23, 17, 5, tzinfo=UTC)
    updated_at = datetime(2026, 6, 23, 18, 30, tzinfo=UTC)
    item = DealRiskBlocker(
        risk_blocker_id=risk_blocker_id,
        organisation_id=organisation_id,
        deal_id=deal_id,
        item_type=RiskBlockerType.BLOCKER,
        title="Procurement reopened",
        category=RiskBlockerCategory.BUDGET,
        severity=RiskBlockerSeverity.MEDIUM,
        status=RiskBlockerStatus.OPEN,
        source_type=RiskBlockerSource.MANUAL,
        updated_at=updated_at,
        risk_blocker_data={
            "audit_trail": [
                {
                    "action": "reopened",
                    "timestamp": audit_at.isoformat(),
                }
            ]
        },
    )
    session = _FakeSession()

    await ActivityFeedProjectionService(session, organisation_id).upsert_risk_blocker_lifecycle(
        item,
        source_type="BLOCKER_REOPENED",
    )

    values = _compiled_values(session.statements[0])
    assert values["occurred_at"] == audit_at
    assert values["source_record_id"] == (
        f"{risk_blocker_id}:BLOCKER_REOPENED:{audit_at.isoformat()}"
    )
