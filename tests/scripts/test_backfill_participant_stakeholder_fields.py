from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts import backfill_participant_stakeholder_fields as backfill


class _FakeContext:
    def __init__(self, session: object) -> None:
        self.session = session

    async def __aenter__(self) -> object:
        return self.session

    async def __aexit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.rolled_back = False

    async def rollback(self) -> None:
        self.rolled_back = True


def test_backfill_prefers_source_reference_id_then_deal_email() -> None:
    stakeholder = {
        "stakeholder_id": "stakeholder-1",
        "deal_id": "deal-1",
        "email": "alex@example.com",
        "source_reference_id": "participant-1",
    }
    participants = [
        {
            "participant_id": "participant-1",
            "deal_id": "deal-1",
            "related_email": "wrong@example.com",
        },
        {
            "participant_id": "participant-2",
            "deal_id": "deal-1",
            "related_email": "alex@example.com",
        },
    ]

    participant = backfill.choose_participant_for_stakeholder(stakeholder, participants)

    assert participant is not None
    assert participant["participant_id"] == "participant-1"


def test_backfill_matches_email_with_trimmed_lowercase_values() -> None:
    stakeholder = {
        "stakeholder_id": "stakeholder-1",
        "deal_id": "deal-1",
        "email": " Alex@Example.com ",
        "source_reference_id": None,
    }
    participants = [
        {
            "participant_id": "participant-2",
            "deal_id": "deal-1",
            "related_email": "alex@example.com",
        },
    ]

    participant = backfill.choose_participant_for_stakeholder(stakeholder, participants)

    assert participant is not None
    assert participant["participant_id"] == "participant-2"


def test_backfill_fills_missing_contact_profile_before_snapshot() -> None:
    stakeholder = {
        "name": "Alex Buyer",
        "email": " Alex@Example.com ",
        "job_title": "CFO",
        "department": "Finance",
        "phone_number": "+1 555 0100",
        "external_links": [{"platform": "linkedin", "url": "https://example.com/alex"}],
    }
    contact = {
        "full_name": None,
        "email": None,
        "job_title": None,
        "phone": None,
        "mobile_phone": None,
        "properties": {},
    }
    participant = {
        "related_name": None,
        "related_email": None,
        "job_title": None,
        "department": None,
        "phone_number": None,
        "external_links": None,
    }

    assert backfill.fill_contact_profile_from_stakeholder(contact, stakeholder) is True
    assert backfill.refresh_participant_profile_snapshot_from_contact(participant, contact) is True

    assert contact["email"] == "alex@example.com"
    assert contact["job_title"] == "CFO"
    assert contact["phone"] == "+1 555 0100"
    assert contact["properties"]["department"] == "Finance"
    assert participant["related_email"] == "alex@example.com"
    assert participant["job_title"] == "CFO"
    assert participant["phone_number"] == "+1 555 0100"


def test_backfill_does_not_overwrite_existing_contact_profile_values() -> None:
    stakeholder = {
        "name": "Stakeholder Name",
        "email": "stakeholder@example.com",
        "job_title": "Stakeholder Title",
        "phone_number": "+1 555 9999",
    }
    contact = {
        "full_name": "Contact Name",
        "email": "contact@example.com",
        "job_title": "Contact Title",
        "phone": "+1 555 0100",
        "properties": {},
    }

    assert backfill.fill_contact_profile_from_stakeholder(contact, stakeholder) is False

    assert contact["full_name"] == "Contact Name"
    assert contact["email"] == "contact@example.com"
    assert contact["job_title"] == "Contact Title"
    assert contact["phone"] == "+1 555 0100"


def test_backfill_copies_stakeholder_research_to_contact_when_missing() -> None:
    researched_at = datetime.now(UTC)
    stakeholder = {
        "stakeholder_research": "Reusable stakeholder research",
        "last_researched_at": researched_at,
    }
    contact = {
        "contact_research": None,
        "last_researched_at": None,
    }

    assert backfill.copy_stakeholder_research_to_contact(contact, stakeholder) is True

    assert contact["contact_research"] == "Reusable stakeholder research"
    assert contact["last_researched_at"] == researched_at


def test_backfill_keeps_newer_contact_research_profile() -> None:
    researched_at = datetime.now(UTC)
    contact_researched_at = researched_at + timedelta(days=1)
    stakeholder = {
        "stakeholder_research": "Older stakeholder research",
        "last_researched_at": researched_at,
    }
    contact = {
        "contact_research": "Newer contact research",
        "last_researched_at": contact_researched_at,
    }

    assert backfill.copy_stakeholder_research_to_contact(contact, stakeholder) is False

    assert contact["contact_research"] == "Newer contact research"
    assert contact["last_researched_at"] == contact_researched_at


def test_backfill_recreates_missing_contact_with_existing_participant_contact_id() -> None:
    contact_id = uuid4()
    stakeholder = SimpleNamespace(
        contact_id=None,
        name="Alex Buyer",
        email="alex@example.com",
        job_title=None,
        phone_number=None,
        department=None,
        external_links=None,
    )

    contact = backfill._new_contact_from_stakeholder(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        stakeholder=stakeholder,
        email_normalized="alex@example.com",
        contact_id=contact_id,
    )

    assert contact.contact_id == contact_id


def test_backfill_copies_only_behavior_fields_to_participant() -> None:
    stakeholder = {
        "stakeholder_id": "legacy-stakeholder-id",
        "created_by_user_id": "user-1",
        "job_title": "CFO",
        "phone_number": "+1 555 0100",
        "stance": "CHAMPION",
        "decision_power": "DECISION_MAKER",
        "champion_potential": 99.0,
        "engagement_score": 91.0,
        "approval_status": "APPROVED",
        "stakeholder_research": "Deal-specific research",
        "stakeholder_engagement_history": [{"temperature": "HOT"}],
        "stakeholder_data": {"manual_overrides": ["job_title"]},
    }
    participant = {
        "participant_id": "participant-1",
        "job_title": "Contact Snapshot Title",
        "phone_number": "+1 555 0000",
    }

    assert backfill.copy_stakeholder_behavior_to_participant(participant, stakeholder) is True

    assert participant["created_by_user_id"] == "user-1"
    assert participant["stance"] == "CHAMPION"
    assert participant["decision_power"] == "DECISION_MAKER"
    assert participant["approval_status"] == "APPROVED"
    assert participant["stakeholder_research"] == "Deal-specific research"
    assert participant["job_title"] == "Contact Snapshot Title"
    assert participant["phone_number"] == "+1 555 0000"
    assert "champion_potential" not in participant
    assert "engagement_score" not in participant
    assert "is_stakeholder" not in participant
    assert "stakeholder_data" not in participant
    assert "stakeholder_engagement_history" not in participant
    assert "legacy_stakeholder_id" not in participant
    assert "canonical_stakeholder_participant_id" not in participant


@pytest.mark.asyncio
async def test_backfill_org_uses_admin_org_session(monkeypatch: pytest.MonkeyPatch) -> None:
    organisation_id = uuid4()
    used_org_ids: list[object] = []
    session = _FakeSession()

    def fake_acquire_org_admin_async_session(org_id):  # noqa: ANN001
        used_org_ids.append(org_id)
        return _FakeContext(session)

    async def no_stakeholders(*args, **kwargs):  # noqa: ANN002, ANN003
        return []

    async def no_participants(*args, **kwargs):  # noqa: ANN002, ANN003
        return []

    async def no_contacts(*args, **kwargs):  # noqa: ANN002, ANN003
        return []

    monkeypatch.setattr(
        backfill,
        "acquire_org_admin_async_session",
        fake_acquire_org_admin_async_session,
    )
    monkeypatch.setattr(backfill, "_load_stakeholders", no_stakeholders)
    monkeypatch.setattr(backfill, "_load_participants", no_participants)
    monkeypatch.setattr(backfill, "_load_contacts", no_contacts)

    stats = await backfill.backfill_org(organisation_id, apply=False, limit=None)

    assert used_org_ids == [organisation_id]
    assert stats.stakeholders_scanned == 0
    assert session.rolled_back is True
