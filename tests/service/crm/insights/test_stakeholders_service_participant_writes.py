"""Regression tests for participant-backed stakeholder write behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from src.api.schemas.crm_models import CreateStakeholderRequest, UpdateStakeholderRequest
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    StakeholderSource,
)
from src.domain.models.organisation_tables.sync.timeline_models import DealTimelineEvent
from src.service.crm.participants import stakeholder_write_service as stakeholder_write_module
from src.service.crm.participants.contracts import StakeholderWriteContext
from src.service.crm.participants.errors import StakeholdersServiceError
from src.service.crm.participants.stakeholder_write_service import StakeholderWriteService


class _FakeResult:
    def __init__(self, *, scalar=None, scalars=None):
        self._scalar = scalar
        self._scalars = scalars or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def first(self):
        return self._scalar

    def all(self):
        return self._scalars


class _WriteSession:
    def __init__(self, results: list[_FakeResult] | None = None):
        self._results = results or []
        self.added: list[object] = []
        self.committed = False
        self.refreshed: list[object] = []

    async def execute(self, _stmt):
        if not self._results:
            return _FakeResult()
        return self._results.pop(0)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, obj: object) -> None:
        self.refreshed.append(obj)


def _participant(
    *,
    org_id: UUID,
    deal_id: UUID,
    integration_id: UUID,
    contact_id: UUID | None = None,
    email: str | None = "buyer@example.com",
    status: str = ApprovalStatus.APPROVED.value,
) -> CRMDealParticipant:
    return CRMDealParticipant(
        organisation_id=org_id,
        integration_id=integration_id,
        deal_id=deal_id,
        participant_id=uuid4(),
        participant_type=CRMParticipantType.CONTACT,
        related_local_id=contact_id,
        related_email=email,
        related_name="Buyer",
        job_title="Old Title",
        linkedin_url="https://linkedin.com/in/old-buyer",
        approval_status=status,
        participant_source=StakeholderSource.MANUAL.value,
        stance="UNKNOWN",
        decision_power="UNKNOWN",
        temperature="UNKNOWN",
        synced_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_create_stakeholder_rejects_conflicting_contact_id_and_email() -> None:
    """Agent payloads can overfill identity fields; strong IDs must agree before writes."""
    org_id = uuid4()
    deal_id = uuid4()
    integration_id = uuid4()
    selected_contact = CRMContact(
        organisation_id=org_id,
        integration_id=integration_id,
        contact_id=uuid4(),
        email="selected@example.com",
        full_name="Selected Buyer",
    )
    email_contact = CRMContact(
        organisation_id=org_id,
        integration_id=integration_id,
        contact_id=uuid4(),
        email="typed@example.com",
        full_name="Typed Buyer",
    )
    service = StakeholderWriteService(_WriteSession(), org_id, user_id=uuid4())  # type: ignore[arg-type]

    async def fake_find_contact_by_id(_contact_id: UUID | None) -> CRMContact:
        return selected_contact

    async def fake_find_contact_by_email(_email: str | None) -> CRMContact:
        return email_contact

    async def fake_ensure_participant(**_kwargs) -> CRMDealParticipant:
        pytest.fail("conflicting identity claims must reject before participant writes")

    service._find_contact_by_id = fake_find_contact_by_id  # type: ignore[method-assign]
    service._find_contact_by_email = fake_find_contact_by_email  # type: ignore[method-assign]
    service._ensure_deal_participant = fake_ensure_participant  # type: ignore[method-assign]

    with pytest.raises(StakeholdersServiceError) as exc:
        await service.create_stakeholder(
            deal_id,
            CreateStakeholderRequest(
                contact_id=selected_contact.contact_id,
                email="typed@example.com",
            ),
        )

    assert exc.value.status_code == 409
    assert "conflicting" in exc.value.message.lower()


@pytest.mark.asyncio
async def test_create_stakeholder_writes_participant_not_deal_stakeholder() -> None:
    org_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    integration_id = uuid4()
    participant = _participant(
        org_id=org_id,
        deal_id=deal_id,
        integration_id=integration_id,
        contact_id=contact_id,
        email="buyer@example.com",
    )
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=integration_id,
        contact_id=contact_id,
        email="buyer@example.com",
    )
    session = _WriteSession([_FakeResult()])
    service = StakeholderWriteService(session, org_id, user_id=uuid4())  # type: ignore[arg-type]

    async def fake_is_internal_email(_email: str) -> bool:
        return False

    async def fake_ensure_contact(**_kwargs) -> CRMContact:
        return contact

    async def fake_ensure_participant(**_kwargs) -> CRMDealParticipant:
        session.add(participant)
        return participant

    service._is_internal_email = fake_is_internal_email  # type: ignore[method-assign]
    service._ensure_contact_for_stakeholder = fake_ensure_contact  # type: ignore[method-assign]
    service._ensure_deal_participant = fake_ensure_participant  # type: ignore[method-assign]

    result = await service.create_stakeholder(
        deal_id,
        CreateStakeholderRequest(
            email=" Buyer@Example.com ",
            name="Buyer",
            job_title="CFO",
            linkedin_url="https://linkedin.com/in/buyer",
        ),
    )

    assert result.participant_id == participant.participant_id
    assert result.email == "buyer@example.com"
    assert result.name == "Buyer"
    assert result.job_title == "Old Title"
    assert result.linkedin_url == "https://linkedin.com/in/old-buyer"
    assert any(isinstance(obj, CRMDealParticipant) for obj in session.added)
    assert not session.committed


@pytest.mark.asyncio
async def test_create_stakeholder_projects_added_activity_with_create_context(
    monkeypatch,
) -> None:
    """Create provenance belongs on the stakeholder-added activity card."""
    org_id = uuid4()
    deal_id = uuid4()
    actor_user_id = uuid4()
    participant = _participant(
        org_id=org_id,
        deal_id=deal_id,
        integration_id=uuid4(),
        email="buyer@example.com",
    )
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=participant.integration_id,
        contact_id=uuid4(),
        email="buyer@example.com",
    )
    session = _WriteSession([_FakeResult()])
    service = StakeholderWriteService(session, org_id, user_id=actor_user_id)  # type: ignore[arg-type]

    async def fake_is_internal_email(_email: str) -> bool:
        return False

    async def fake_ensure_contact(**_kwargs) -> CRMContact:
        return contact

    async def fake_ensure_participant(**_kwargs) -> CRMDealParticipant:
        return participant

    service._is_internal_email = fake_is_internal_email  # type: ignore[method-assign]
    service._ensure_contact_for_stakeholder = fake_ensure_contact  # type: ignore[method-assign]
    service._ensure_deal_participant = fake_ensure_participant  # type: ignore[method-assign]
    projected: list[dict[str, object]] = []

    class CapturingActivityFeedProjectionService:
        """Capture stakeholder-added projection calls without needing activity tables."""

        def __init__(self, _session, organisation_id: UUID):
            assert organisation_id == org_id

        async def upsert_stakeholder_added(self, participant, **kwargs):
            projected.append({"participant": participant, **kwargs})

    monkeypatch.setattr(
        stakeholder_write_module,
        "ActivityFeedProjectionService",
        CapturingActivityFeedProjectionService,
        raising=False,
    )

    await service.create_stakeholder(
        deal_id,
        CreateStakeholderRequest(email="buyer@example.com"),
        context=StakeholderWriteContext(
            source="agent",
            actor_user_id=actor_user_id,
            created_via="add_stakeholder_agent",
        ),
    )

    assert projected == [
        {
            "participant": participant,
            "contact": contact,
            "actor_user_id": actor_user_id,
            "source": "agent",
            "source_label": None,
            "created_via": "add_stakeholder_agent",
        }
    ]
    assert not any(isinstance(obj, DealTimelineEvent) for obj in session.added)


@pytest.mark.asyncio
async def test_create_stakeholder_allows_name_only_without_creating_contact() -> None:
    org_id = uuid4()
    deal_id = uuid4()
    integration_id = uuid4()
    participant = _participant(
        org_id=org_id,
        deal_id=deal_id,
        integration_id=integration_id,
        email=None,
    )
    participant.related_name = "Jane Buyer"
    participant.related_local_id = None
    session = _WriteSession([_FakeResult()])
    service = StakeholderWriteService(session, org_id, user_id=uuid4())  # type: ignore[arg-type]
    ensure_contact_calls = 0

    async def fake_ensure_contact(**_kwargs) -> UUID:
        nonlocal ensure_contact_calls
        ensure_contact_calls += 1
        return uuid4()

    async def fake_ensure_participant(**_kwargs) -> CRMDealParticipant:
        assert _kwargs["email"] is None
        assert _kwargs["contact_id"] is None
        session.add(participant)
        return participant

    service._ensure_contact_for_stakeholder = fake_ensure_contact  # type: ignore[method-assign]
    service._ensure_deal_participant = fake_ensure_participant  # type: ignore[method-assign]

    result = await service.create_stakeholder(
        deal_id,
        CreateStakeholderRequest(name="Jane Buyer"),
    )

    assert result.participant_id == participant.participant_id
    assert result.email is None
    assert result.name == "Jane Buyer"
    assert ensure_contact_calls == 0
    assert any(isinstance(obj, CRMDealParticipant) for obj in session.added)


@pytest.mark.asyncio
async def test_create_stakeholder_allows_linkedin_only_without_email() -> None:
    org_id = uuid4()
    deal_id = uuid4()
    integration_id = uuid4()
    linkedin_url = "https://linkedin.com/in/jane-buyer"
    participant = _participant(
        org_id=org_id,
        deal_id=deal_id,
        integration_id=integration_id,
        email=None,
    )
    participant.linkedin_url = linkedin_url
    participant.related_local_id = None
    session = _WriteSession([_FakeResult()])
    service = StakeholderWriteService(session, org_id, user_id=uuid4())  # type: ignore[arg-type]

    async def fake_find_contact_by_linkedin_url(_linkedin_url: str | None) -> None:
        assert _linkedin_url == linkedin_url
        return None

    async def fake_ensure_participant(**_kwargs) -> CRMDealParticipant:
        assert _kwargs["email"] is None
        assert _kwargs["contact_id"] is None
        assert _kwargs["linkedin_url"] == linkedin_url
        session.add(participant)
        return participant

    service._find_contact_by_linkedin_url = fake_find_contact_by_linkedin_url  # type: ignore[method-assign]
    service._ensure_deal_participant = fake_ensure_participant  # type: ignore[method-assign]

    result = await service.create_stakeholder(
        deal_id,
        CreateStakeholderRequest(linkedin_url=linkedin_url),
    )

    assert result.email is None
    assert result.linkedin_url == linkedin_url
    assert any(isinstance(obj, CRMDealParticipant) for obj in session.added)


@pytest.mark.asyncio
async def test_create_stakeholder_from_linkedin_uses_existing_contact_profile() -> None:
    """LinkedIn matches should use the same contact profile path as contact_id creates."""
    org_id = uuid4()
    deal_id = uuid4()
    integration_id = uuid4()
    linkedin_url = "https://linkedin.com/in/jane-buyer"
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=integration_id,
        contact_id=uuid4(),
        email="jane@buyer.example",
        full_name="Jane Buyer",
        job_title="VP Finance",
        phone="+1 555-0101",
        linkedin_url=linkedin_url,
        contact_research="Existing contact research",
        last_researched_at=datetime.now(UTC),
        properties={"department": "Finance"},
    )
    participant = _participant(
        org_id=org_id,
        deal_id=deal_id,
        integration_id=integration_id,
        contact_id=contact.contact_id,
        email=contact.email,
    )
    session = _WriteSession(
        [_FakeResult(scalar=contact), _FakeResult(), _FakeResult(scalar=contact)]
    )
    service = StakeholderWriteService(session, org_id, user_id=uuid4())  # type: ignore[arg-type]

    async def fake_is_internal_email(_email: str) -> bool:
        return False

    async def fake_ensure_participant(**kwargs) -> CRMDealParticipant:
        assert kwargs["contact_id"] == contact.contact_id
        assert kwargs["email"] == "jane@buyer.example"
        assert kwargs["name"] == "Jane Buyer"
        assert kwargs["job_title"] == "VP Finance"
        assert kwargs["phone_number"] == "+1 555-0101"
        assert kwargs["linkedin_url"] == linkedin_url
        assert kwargs["department"] == "Finance"
        participant.related_local_id = kwargs["contact_id"]
        participant.related_email = kwargs["email"]
        participant.related_name = kwargs["name"]
        participant.job_title = kwargs["job_title"]
        participant.phone_number = kwargs["phone_number"]
        participant.linkedin_url = kwargs["linkedin_url"]
        participant.department = kwargs["department"]
        return participant

    service._is_internal_email = fake_is_internal_email  # type: ignore[method-assign]
    service._ensure_deal_participant = fake_ensure_participant  # type: ignore[method-assign]

    result = await service.create_stakeholder(
        deal_id,
        CreateStakeholderRequest(linkedin_url=linkedin_url),
    )

    assert result.contact_id == contact.contact_id
    assert result.email == "jane@buyer.example"
    assert result.name == "Jane Buyer"
    assert result.stakeholder_research == "Existing contact research"
    assert participant.stakeholder_research == "Existing contact research"


@pytest.mark.asyncio
async def test_create_stakeholder_from_email_uses_existing_contact_profile() -> None:
    """Existing email contacts should hydrate before the create-contact fallback."""
    org_id = uuid4()
    deal_id = uuid4()
    integration_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=integration_id,
        contact_id=uuid4(),
        email="jane@buyer.example",
        full_name="Jane Buyer",
        job_title="VP Finance",
        phone="+1 555-0101",
        linkedin_url="https://linkedin.com/in/jane-buyer",
        contact_research="Existing contact research",
        last_researched_at=datetime.now(UTC),
        properties={"department": "Finance"},
    )
    participant = _participant(
        org_id=org_id,
        deal_id=deal_id,
        integration_id=integration_id,
        contact_id=contact.contact_id,
        email=contact.email,
    )
    session = _WriteSession([_FakeResult(), _FakeResult(scalar=contact)])
    service = StakeholderWriteService(session, org_id, user_id=uuid4())  # type: ignore[arg-type]

    async def fake_is_internal_email(_email: str) -> bool:
        return False

    async def fake_find_contact_by_email(email: str | None) -> CRMContact:
        assert email == "jane@buyer.example"
        return contact

    async def fake_ensure_contact(**_kwargs) -> CRMContact:
        pytest.fail("existing email contacts should be found before contact creation fallback")

    async def fake_ensure_participant(**kwargs) -> CRMDealParticipant:
        assert kwargs["contact_id"] == contact.contact_id
        assert kwargs["email"] == "jane@buyer.example"
        assert kwargs["name"] == "Jane Buyer"
        assert kwargs["job_title"] == "VP Finance"
        assert kwargs["phone_number"] == "+1 555-0101"
        assert kwargs["linkedin_url"] == "https://linkedin.com/in/jane-buyer"
        assert kwargs["department"] == "Finance"
        participant.related_local_id = kwargs["contact_id"]
        participant.related_email = kwargs["email"]
        participant.related_name = kwargs["name"]
        participant.job_title = kwargs["job_title"]
        participant.phone_number = kwargs["phone_number"]
        participant.linkedin_url = kwargs["linkedin_url"]
        participant.department = kwargs["department"]
        return participant

    service._is_internal_email = fake_is_internal_email  # type: ignore[method-assign]
    service._find_contact_by_email = fake_find_contact_by_email  # type: ignore[method-assign]
    service._ensure_contact_for_stakeholder = fake_ensure_contact  # type: ignore[method-assign]
    service._ensure_deal_participant = fake_ensure_participant  # type: ignore[method-assign]

    result = await service.create_stakeholder(
        deal_id,
        CreateStakeholderRequest(email="Jane@Buyer.Example"),
    )

    assert result.contact_id == contact.contact_id
    assert result.email == "jane@buyer.example"
    assert result.name == "Jane Buyer"
    assert result.stakeholder_research == "Existing contact research"
    assert participant.stakeholder_research == "Existing contact research"


@pytest.mark.asyncio
async def test_create_stakeholder_does_not_extract_linkedin_from_external_links() -> None:
    """Create uses only the explicit linkedin_url field, not generic external links."""
    org_id = uuid4()
    deal_id = uuid4()
    participant = _participant(
        org_id=org_id,
        deal_id=deal_id,
        integration_id=uuid4(),
        email=None,
    )
    participant.related_name = "Jane Buyer"
    participant.linkedin_url = None
    session = _WriteSession([_FakeResult()])
    service = StakeholderWriteService(session, org_id, user_id=uuid4())  # type: ignore[arg-type]

    async def fake_find_contact_by_linkedin_url(_linkedin_url: str | None) -> None:
        pytest.fail("external_links should not be used for LinkedIn matching on create")

    async def fake_ensure_participant(**_kwargs) -> CRMDealParticipant:
        assert _kwargs["linkedin_url"] is None
        assert _kwargs["external_links"] is None
        session.add(participant)
        return participant

    service._find_contact_by_linkedin_url = fake_find_contact_by_linkedin_url  # type: ignore[method-assign]
    service._ensure_deal_participant = fake_ensure_participant  # type: ignore[method-assign]

    result = await service.create_stakeholder(
        deal_id,
        CreateStakeholderRequest(name="Jane Buyer"),
    )

    assert result.linkedin_url is None


@pytest.mark.asyncio
async def test_create_stakeholder_from_contact_id_copies_contact_profile() -> None:
    org_id = uuid4()
    deal_id = uuid4()
    integration_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=integration_id,
        contact_id=uuid4(),
        email="buyer@example.com",
        full_name="Existing Buyer",
        job_title="VP Finance",
        phone="+1 555-0101",
        linkedin_url="https://linkedin.com/in/existing-buyer",
        contact_research="Existing research",
        last_researched_at=datetime.now(UTC),
        properties={
            "department": "Finance",
            "external_links": [
                {"platform": "linkedin", "url": "https://linkedin.com/in/existing-buyer"}
            ],
        },
    )
    participant = _participant(
        org_id=org_id,
        deal_id=deal_id,
        integration_id=integration_id,
        contact_id=contact.contact_id,
        email=contact.email,
    )
    session = _WriteSession(
        [
            _FakeResult(scalar=contact),
            _FakeResult(),
            _FakeResult(scalar=contact),
        ]
    )
    service = StakeholderWriteService(session, org_id, user_id=uuid4())  # type: ignore[arg-type]

    async def fake_is_internal_email(_email: str) -> bool:
        return False

    async def fake_ensure_participant(**kwargs) -> CRMDealParticipant:
        assert kwargs["contact_id"] == contact.contact_id
        assert kwargs["email"] == "buyer@example.com"
        assert kwargs["name"] == "Existing Buyer"
        assert kwargs["job_title"] == "VP Finance"
        assert kwargs["phone_number"] == "+1 555-0101"
        assert kwargs["linkedin_url"] == "https://linkedin.com/in/existing-buyer"
        assert kwargs["department"] == "Finance"
        assert kwargs["external_links"] is None
        participant.related_name = kwargs["name"]
        participant.job_title = kwargs["job_title"]
        participant.phone_number = kwargs["phone_number"]
        participant.linkedin_url = kwargs["linkedin_url"]
        participant.department = kwargs["department"]
        participant.external_links = kwargs["external_links"]
        return participant

    service._is_internal_email = fake_is_internal_email  # type: ignore[method-assign]
    service._ensure_deal_participant = fake_ensure_participant  # type: ignore[method-assign]

    result = await service.create_stakeholder(
        deal_id,
        CreateStakeholderRequest(contact_id=contact.contact_id),
    )

    assert result.contact_id == contact.contact_id
    assert result.email == "buyer@example.com"
    assert result.name == "Existing Buyer"
    assert result.job_title == "VP Finance"
    assert result.linkedin_url == "https://linkedin.com/in/existing-buyer"
    assert participant.stakeholder_research == "Existing research"
    assert participant.last_researched_at == contact.last_researched_at


@pytest.mark.asyncio
async def test_create_stakeholder_updates_existing_participant_without_approval_gate() -> None:
    """Existing active participants should be updated in place instead of conflicted."""
    org_id = uuid4()
    deal_id = uuid4()
    participant = _participant(
        org_id=org_id,
        deal_id=deal_id,
        integration_id=uuid4(),
        email="old@example.com",
        status=ApprovalStatus.APPROVED.value,
    )
    session = _WriteSession([_FakeResult()])
    service = StakeholderWriteService(session, org_id, user_id=uuid4())  # type: ignore[arg-type]

    async def fake_is_internal_email(_email: str) -> bool:
        return False

    async def fake_ensure_contact(**_kwargs) -> None:
        return None

    async def fake_find_participant(**_kwargs) -> CRMDealParticipant:
        return participant

    async def fake_ensure_participant(**_kwargs) -> CRMDealParticipant:
        pytest.fail("existing participant should be updated, not recreated")

    service._is_internal_email = fake_is_internal_email  # type: ignore[method-assign]
    service._ensure_contact_for_stakeholder = fake_ensure_contact  # type: ignore[method-assign]
    service._find_contact_participant = fake_find_participant  # type: ignore[method-assign]
    service._ensure_deal_participant = fake_ensure_participant  # type: ignore[method-assign]

    result = await service.create_stakeholder(
        deal_id,
        CreateStakeholderRequest(
            email="new@example.com",
            name="New Buyer",
            job_title="CFO",
        ),
    )

    assert result.participant_id == participant.participant_id
    assert participant.related_email == "new@example.com"
    assert participant.related_name == "New Buyer"
    assert participant.job_title == "CFO"
    assert participant.approval_status == ApprovalStatus.APPROVED.value
    assert participant.is_deleted is False
    assert participant.updated_at is not None


@pytest.mark.asyncio
async def test_update_stakeholder_updates_contact_profile_and_participant_behavior() -> None:
    org_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    participant = _participant(
        org_id=org_id,
        deal_id=deal_id,
        integration_id=uuid4(),
        contact_id=contact_id,
    )
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=contact_id,
        email="buyer@example.com",
        full_name="Old Buyer",
        job_title="Old Title",
        phone="111",
        linkedin_url="https://linkedin.com/in/old-buyer",
    )
    session = _WriteSession([_FakeResult(scalar=participant), _FakeResult(scalar=contact)])
    service = StakeholderWriteService(session, org_id)  # type: ignore[arg-type]

    result = await service.update_stakeholder(
        deal_id,
        participant.participant_id,
        UpdateStakeholderRequest(
            name="New Buyer",
            job_title="CFO",
            phone_number="222",
            linkedin_url="https://linkedin.com/in/new-buyer",
            stance="CHAMPION",
            motivation="Owns budget",
        ),
    )

    assert result.participant_id == participant.participant_id
    assert contact.full_name == "New Buyer"
    assert contact.job_title == "CFO"
    assert contact.phone == "222"
    assert contact.linkedin_url == "https://linkedin.com/in/new-buyer"
    assert participant.related_name == "New Buyer"
    assert participant.job_title == "CFO"
    assert participant.phone_number == "222"
    assert participant.linkedin_url == "https://linkedin.com/in/new-buyer"
    assert participant.stance == "CHAMPION"
    assert participant.motivation == "Owns budget"
    assert not session.committed


@pytest.mark.asyncio
async def test_remove_stakeholder_soft_deletes_without_approval_gate(monkeypatch) -> None:
    org_id = uuid4()
    deal_id = uuid4()
    participant = _participant(
        org_id=org_id,
        deal_id=deal_id,
        integration_id=uuid4(),
        status=ApprovalStatus.PENDING.value,
    )
    session = _WriteSession([_FakeResult(scalar=participant)])
    service = StakeholderWriteService(session, org_id)  # type: ignore[arg-type]
    projected_removals: list[dict[str, object]] = []

    class CapturingActivityFeedProjectionService:
        """Capture stakeholder removal projection calls without activity tables."""

        def __init__(self, _session, organisation_id: UUID):
            assert organisation_id == org_id

        async def upsert_stakeholder_removed(self, participant, **kwargs):
            projected_removals.append({"participant": participant, **kwargs})

    monkeypatch.setattr(
        stakeholder_write_module,
        "ActivityFeedProjectionService",
        CapturingActivityFeedProjectionService,
        raising=False,
    )

    removed = await service.remove_stakeholder(deal_id, participant.participant_id)

    assert removed.participant_id == participant.participant_id
    assert participant.approval_status == ApprovalStatus.PENDING.value
    assert participant.is_deleted is True
    assert projected_removals == [
        {
            "participant": participant,
            "contact": None,
            "actor_user_id": service.user_id,
            "source": "api",
            "source_label": "API",
            "created_via": "remove_stakeholder",
        }
    ]
    assert not any(isinstance(obj, DealTimelineEvent) for obj in session.added)


@pytest.mark.asyncio
async def test_add_contacts_as_stakeholders_writes_participants() -> None:
    org_id = uuid4()
    deal_id = uuid4()
    integration_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=integration_id,
        contact_id=uuid4(),
        email="contact@example.com",
        full_name="Contact Buyer",
        job_title="VP Sales",
        phone="+1 555-0101",
        linkedin_url="https://linkedin.com/in/contact-buyer",
    )
    participant = _participant(
        org_id=org_id,
        deal_id=deal_id,
        integration_id=integration_id,
        contact_id=contact.contact_id,
        email="contact@example.com",
    )
    session = _WriteSession([_FakeResult(scalars=[contact]), _FakeResult(scalars=[])])
    service = StakeholderWriteService(session, org_id, user_id=uuid4())  # type: ignore[arg-type]
    projected: list[dict[str, object]] = []

    async def fake_ensure_participant(**_kwargs) -> CRMDealParticipant:
        session.add(participant)
        return participant

    service._ensure_deal_participant = fake_ensure_participant  # type: ignore[method-assign]
    original_projection = stakeholder_write_module.ActivityFeedProjectionService

    class CapturingActivityFeedProjectionService:
        """Capture selected-contact activity projection calls in the batch-add path."""

        def __init__(self, _session, organisation_id: UUID):
            assert organisation_id == org_id

        async def upsert_stakeholder_added(self, participant, **kwargs):
            projected.append({"participant": participant, **kwargs})

    stakeholder_write_module.ActivityFeedProjectionService = CapturingActivityFeedProjectionService

    try:
        result = await service.add_contacts_as_stakeholders(deal_id, [contact.contact_id])
    finally:
        stakeholder_write_module.ActivityFeedProjectionService = original_projection

    assert result.total_added == 1
    assert result.total_skipped == 0
    assert result.added[0].participant_id == participant.participant_id
    assert result.added[0].linkedin_url == "https://linkedin.com/in/contact-buyer"
    assert any(isinstance(obj, CRMDealParticipant) for obj in session.added)
    assert len(projected) == 1
    assert projected[0]["participant"] is participant
    assert isinstance(projected[0]["contact"], CRMContact)
    assert projected[0]["contact"].email == "contact@example.com"
    assert projected[0]["actor_user_id"] == service.user_id
    assert projected[0]["source"] == "api"
    assert projected[0]["source_label"] == "API"
    assert projected[0]["created_via"] == "stakeholder_add_contacts"
    assert not any(isinstance(obj, DealTimelineEvent) for obj in session.added)
    assert not session.committed


@pytest.mark.asyncio
async def test_add_contacts_as_stakeholders_dedupes_whitespace_padded_email() -> None:
    org_id = uuid4()
    deal_id = uuid4()
    integration_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=integration_id,
        contact_id=uuid4(),
        email=" Buyer@Example.com ",
        full_name="Buyer",
    )
    existing_participant = _participant(
        org_id=org_id,
        deal_id=deal_id,
        integration_id=integration_id,
        email="buyer@example.com",
    )
    session = _WriteSession(
        [
            _FakeResult(scalars=[contact]),
            _FakeResult(scalars=[existing_participant]),
        ]
    )
    service = StakeholderWriteService(session, org_id, user_id=uuid4())  # type: ignore[arg-type]

    result = await service.add_contacts_as_stakeholders(deal_id, [contact.contact_id])

    assert result.total_added == 0
    assert result.total_skipped == 1
    assert result.skipped[0].contact_id == contact.contact_id
    assert result.skipped[0].reason == "Stakeholder with this email already exists"
    assert session.added == []
    assert not session.committed


@pytest.mark.asyncio
async def test_add_contacts_as_stakeholders_without_user_skips_internal_setup(
    monkeypatch,
) -> None:
    """Batch contact add should not enter internal CRM setup without an actor user."""
    org_id = uuid4()
    deal_id = uuid4()
    integration_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=integration_id,
        contact_id=uuid4(),
        email="buyer@example.com",
        full_name="Buyer",
    )
    session = _WriteSession([_FakeResult(scalars=[contact]), _FakeResult(scalars=[])])
    service = StakeholderWriteService(session, org_id)  # type: ignore[arg-type]

    class FailingInternalCRMSetupService:
        """Fail if the no-user path reaches internal CRM provisioning."""

        def __init__(self, *_args, **_kwargs):
            raise AssertionError("internal CRM setup requires a concrete user_id")

    monkeypatch.setattr(
        stakeholder_write_module,
        "InternalCRMSetupService",
        FailingInternalCRMSetupService,
        raising=False,
    )

    result = await service.add_contacts_as_stakeholders(deal_id, [contact.contact_id])

    assert result.total_added == 0
    assert result.total_skipped == 1
    assert result.skipped[0].reason == "Unable to create stakeholder participant"
    assert session.added == []


@pytest.mark.asyncio
async def test_add_contacts_as_stakeholders_copies_external_contact_to_internal_crm(
    monkeypatch,
) -> None:
    org_id = uuid4()
    deal_id = uuid4()
    external_integration_id = uuid4()
    internal_integration_id = uuid4()
    external_contact_id = uuid4()
    external_contact = CRMContact(
        organisation_id=org_id,
        integration_id=external_integration_id,
        contact_id=external_contact_id,
        email="buyer@example.com",
        full_name="Buyer User",
        job_title="VP Sales",
        company_name="External Co",
        company_id=uuid4(),
        phone="+1 555-0101",
        linkedin_url="https://linkedin.com/in/buyer",
    )
    session = _WriteSession(
        [
            _FakeResult(scalars=[external_contact]),
            _FakeResult(scalars=[]),
            _FakeResult(scalars=[]),
        ]
    )
    service = StakeholderWriteService(session, org_id, user_id=uuid4())  # type: ignore[arg-type]
    captured_contact_ids: list[UUID | None] = []

    class FakeInternalCRMSetupService:
        def __init__(self, *_args, **_kwargs):
            pass

        async def setup_internal_crm(self, *, commit: bool):
            assert commit is False
            return SimpleNamespace(
                integration=SimpleNamespace(integration_id=internal_integration_id)
            )

    async def fake_ensure_participant(**kwargs) -> CRMDealParticipant:
        captured_contact_ids.append(kwargs["contact_id"])
        return _participant(
            org_id=org_id,
            deal_id=deal_id,
            integration_id=internal_integration_id,
            contact_id=kwargs["contact_id"],
            email=kwargs["email"],
        )

    monkeypatch.setattr(
        stakeholder_write_module,
        "InternalCRMSetupService",
        FakeInternalCRMSetupService,
        raising=False,
    )
    service._ensure_deal_participant = fake_ensure_participant  # type: ignore[method-assign]

    result = await service.add_contacts_as_stakeholders(deal_id, [external_contact_id])

    copied_contacts = [obj for obj in session.added if isinstance(obj, CRMContact)]
    assert result.total_added == 1
    assert len(copied_contacts) == 1
    assert copied_contacts[0].contact_id != external_contact_id
    assert copied_contacts[0].integration_id == internal_integration_id
    assert copied_contacts[0].company_name == "External Co"
    assert copied_contacts[0].company_id is None
    assert copied_contacts[0].properties["copied_from_contact_id"] == str(external_contact_id)
    assert captured_contact_ids == [copied_contacts[0].contact_id]
