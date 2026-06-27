from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
import src.api.routers.crm.contacts_handler as contacts_handler
from fastapi import Response as FastAPIResponse
from pydantic import ValidationError
from src.api.routers.crm.contacts_handler import (
    create_contact,
    get_contact_detail,
    list_contacts,
    search_contact_create_candidates,
)
from src.api.schemas.crm_contact_models import CreateContactRequest
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMDealStatus,
    CRMParticipantType,
)
from src.service.crm.contacts.contracts import ContactCreateCommand, ContactWriteResult


class _FakeContactReadService:
    def __init__(self, rows: list[dict], total_count: int):
        self.rows = rows
        self.total_count = total_count

    async def list_contacts(self, **kwargs):
        self.request = kwargs
        return self.rows, self.total_count


class _FakeContactSearchService:
    def __init__(self, rows: list[dict], next_cursor: str | None):
        self.rows = rows
        self.next_cursor = next_cursor

    async def search_contacts(self, **kwargs):
        self.request = kwargs
        return self.rows, self.next_cursor

    async def search_contacts_for_create(self, **kwargs):
        self.request = kwargs
        return self.rows, self.next_cursor


class _FakeContactDetailService:
    def __init__(self, result: dict):
        self.result = result

    async def get_contact_detail(self, contact_id):
        self.contact_id = contact_id
        return self.result


class _FakeContactWriteService:
    def __init__(self, contact: CRMContact):
        self.contact = contact
        self.org_session = SimpleNamespace(commit=_AsyncCall(), rollback=_AsyncCall())
        self.calls: list[dict] = []

    async def create_contact(self, command: ContactCreateCommand, **kwargs):
        self.calls.append({"command": command, **kwargs})
        return ContactWriteResult(contact=self.contact, created=True, matched_by="created")


class _AsyncCall:
    def __init__(self) -> None:
        self.count = 0

    async def __call__(self) -> None:
        self.count += 1


def test_create_contact_request_accepts_email_without_name() -> None:
    request = CreateContactRequest(
        email="buyer@example.com",
        job_title="CFO",
        linkedin_url="https://linkedin.com/in/buyer",
    )

    assert request.email == "buyer@example.com"
    assert request.name is None
    assert request.job_title == "CFO"
    assert request.linkedin_url == "https://linkedin.com/in/buyer"


def test_create_contact_request_rejects_removed_api_fields() -> None:
    with pytest.raises(ValidationError):
        CreateContactRequest(
            email="buyer@example.com",
            name="Buyer",
            phone="+15555550100",
        )


@pytest.mark.asyncio
async def test_create_contact_handler_passes_create_command_to_service(monkeypatch) -> None:
    org_id = uuid4()
    user_id = uuid4()
    now = datetime(2026, 6, 25, 10, 0, tzinfo=UTC)
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="buyer@example.com",
        full_name=None,
        created_at=now,
        updated_at=now,
    )
    service = _FakeContactWriteService(contact)

    async def noop_dispatch(**_kwargs):
        return None

    monkeypatch.setattr(
        contacts_handler,
        "dispatch_contact_research_async",
        noop_dispatch,
        raising=False,
    )

    result = await create_contact(
        body=CreateContactRequest(
            email="buyer@example.com",
            job_title="CFO",
            linkedin_url="https://linkedin.com/in/buyer",
        ),
        response=FastAPIResponse(),
        service=service,  # type: ignore[arg-type]
        user_ids=SimpleNamespace(user_id=user_id, organisation_id=org_id),
        _crm_write_guard=None,
        _rl_write=None,
    )

    assert result.data.email == "buyer@example.com"  # type: ignore[union-attr]
    command = service.calls[0]["command"]
    assert command == ContactCreateCommand(
        email="buyer@example.com",
        job_title="CFO",
        linkedin_url="https://linkedin.com/in/buyer",
    )
    assert "phone" not in service.calls[0]
    assert "company_id" not in service.calls[0]
    assert service.org_session.commit.count == 1


@pytest.mark.asyncio
async def test_create_contact_handler_dispatches_research_after_commit(monkeypatch) -> None:
    org_id = uuid4()
    user_id = uuid4()
    now = datetime(2026, 6, 25, 10, 0, tzinfo=UTC)
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="buyer@example.com",
        created_at=now,
        updated_at=now,
    )
    service = _FakeContactWriteService(contact)
    calls: list[dict] = []

    async def fake_dispatch(**kwargs):
        calls.append(
            {
                **kwargs,
                "commit_count_at_dispatch": service.org_session.commit.count,
            }
        )

    monkeypatch.setattr(
        contacts_handler,
        "dispatch_contact_research_async",
        fake_dispatch,
        raising=False,
    )

    result = await create_contact(
        body=CreateContactRequest(email="buyer@example.com"),
        response=FastAPIResponse(),
        service=service,  # type: ignore[arg-type]
        user_ids=SimpleNamespace(user_id=user_id, organisation_id=org_id),
        _crm_write_guard=None,
        _rl_write=None,
    )

    assert result.data.email == "buyer@example.com"  # type: ignore[union-attr]
    assert calls == [
        {
            "organisation_id": org_id,
            "contact_id": contact.contact_id,
            "user_id": user_id,
            "trigger": "contact_created",
            "commit_count_at_dispatch": 1,
        }
    ]


@pytest.mark.asyncio
async def test_create_contact_handler_dispatches_account_research_for_created_account(
    monkeypatch,
) -> None:
    org_id = uuid4()
    user_id = uuid4()
    company_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="buyer@example.com",
    )
    service = _FakeContactWriteService(contact)

    async def create_contact_with_account(*args, **kwargs):
        result = await _FakeContactWriteService.create_contact(service, *args, **kwargs)
        return ContactWriteResult(
            contact=result.contact,
            created=True,
            matched_by="created",
            created_account_id=company_id,
        )

    service.create_contact = create_contact_with_account
    contact_calls: list[dict] = []
    account_calls: list[dict] = []

    async def fake_contact_dispatch(**kwargs):
        contact_calls.append(kwargs)

    async def fake_account_dispatch(**kwargs):
        account_calls.append(
            {
                **kwargs,
                "commit_count_at_dispatch": service.org_session.commit.count,
            }
        )

    monkeypatch.setattr(
        contacts_handler,
        "dispatch_contact_research_async",
        fake_contact_dispatch,
        raising=False,
    )
    monkeypatch.setattr(
        contacts_handler,
        "dispatch_account_research_async",
        fake_account_dispatch,
        raising=False,
    )

    result = await create_contact(
        body=CreateContactRequest(email="buyer@example.com"),
        response=FastAPIResponse(),
        service=service,  # type: ignore[arg-type]
        user_ids=SimpleNamespace(user_id=user_id, organisation_id=org_id),
        _crm_write_guard=None,
        _rl_write=None,
    )

    assert result.status == "SUCCESS"
    assert len(contact_calls) == 1
    assert account_calls == [
        {
            "organisation_id": org_id,
            "company_id": company_id,
            "user_id": user_id,
            "trigger": "contact_created_account",
            "commit_count_at_dispatch": 1,
        }
    ]


@pytest.mark.asyncio
async def test_create_contact_handler_ignores_research_dispatch_failure(monkeypatch) -> None:
    org_id = uuid4()
    user_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="buyer@example.com",
    )
    service = _FakeContactWriteService(contact)

    async def failing_dispatch(**_kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(
        contacts_handler,
        "dispatch_contact_research_async",
        failing_dispatch,
        raising=False,
    )

    result = await create_contact(
        body=CreateContactRequest(email="buyer@example.com"),
        response=FastAPIResponse(),
        service=service,  # type: ignore[arg-type]
        user_ids=SimpleNamespace(user_id=user_id, organisation_id=org_id),
        _crm_write_guard=None,
        _rl_write=None,
    )

    assert result.data.email == "buyer@example.com"  # type: ignore[union-attr]
    assert service.org_session.commit.count == 1
    assert service.org_session.rollback.count == 0


@pytest.mark.asyncio
async def test_list_contacts_returns_new_list_only_contract() -> None:
    contact_id = uuid4()
    company_id = uuid4()
    owner_id = uuid4()
    now = datetime(2026, 6, 20, 15, 45, tzinfo=UTC)
    enriched_at = datetime(2026, 6, 19, 8, 15, tzinfo=UTC)
    contact = CRMContact(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        contact_id=contact_id,
        first_name="Jane",
        last_name="Doe",
        full_name="Jane Doe",
        email="jane@example.com",
        additional_emails=["jane.alt@example.com"],
        job_title="VP Sales",
        phone="+1 415 555 0100",
        mobile_phone="+1 415 555 0101",
        linkedin_url="https://linkedin.com/in/janedoe",
        lifecycle_stage="lead",
        lead_status="new",
        marketing_status="subscribed",
        profile_enrichment={"headline": "Sales leader"},
        profile_enriched_at=enriched_at,
        last_contacted_at=now,
        last_activity_at=now,
        created_at=now,
        updated_at=now,
    )
    service = _FakeContactReadService(
        rows=[
            {
                "contact": contact,
                "deal_count": 3,
                "source": "hubspot",
                "company": {
                    "id": company_id,
                    "name": "Example Corp",
                    "domain": "example.com",
                },
                "owner": {
                    "id": owner_id,
                    "name": "Nikhil Sharma",
                    "email": "nikhil@example.com",
                },
            }
        ],
        total_count=1,
    )

    result = await list_contacts(
        response=FastAPIResponse(),
        page=1,
        limit=25,
        q=None,
        company_id=None,
        service=service,  # type: ignore[arg-type]
        _rl_read=None,
    )

    item = result.data.items[0]  # type: ignore[union-attr]
    dumped = item.model_dump()
    assert dumped == {
        "contact_id": contact_id,
        "full_name": "Jane Doe",
        "email": "jane@example.com",
        "additional_emails": ["jane.alt@example.com"],
        "job_title": "VP Sales",
        "phone": "+1 415 555 0100",
        "mobile_phone": "+1 415 555 0101",
        "linkedin_url": "https://linkedin.com/in/janedoe",
        "company": {
            "id": company_id,
            "name": "Example Corp",
            "domain": "example.com",
        },
        "owner": {
            "id": owner_id,
            "name": "Nikhil Sharma",
            "email": "nikhil@example.com",
        },
        "deal_count": 3,
        "last_contacted_at": now,
        "last_activity_at": now,
        "source": "hubspot",
        "is_profile_enriched": True,
        "profile_enriched_at": enriched_at,
        "created_at": now,
        "updated_at": now,
    }
    assert "first_name" not in dumped
    assert "company_id" not in dumped
    assert "owner_name" not in dumped
    assert "lifecycle_stage" not in dumped
    assert "profile_enrichment_source" not in dumped


@pytest.mark.asyncio
async def test_search_contacts_returns_cursor_page() -> None:
    contact_id = uuid4()
    company_id = uuid4()
    service = _FakeContactSearchService(
        rows=[
            {
                "contact_id": contact_id,
                "full_name": "Sam Altman",
                "email": "sam.altman@openai.com",
                "job_title": "CEO",
                "company": {
                    "id": company_id,
                    "name": "OpenAI",
                    "domain": "openai.com",
                },
                "match_type": "exact_email",
            }
        ],
        next_cursor="next-token",
    )

    result = await contacts_handler.search_contacts(
        response=FastAPIResponse(),
        q="sam.altman@openai.com",
        limit=20,
        cursor=None,
        company_id=None,
        service=service,  # type: ignore[arg-type]
        _rl_read=None,
    )

    assert service.request == {
        "q": "sam.altman@openai.com",
        "limit": 20,
        "cursor": None,
        "company_id": None,
    }
    assert result.status == "SUCCESS"
    assert result.data is not None
    assert result.data.items[0].contact_id == contact_id
    assert result.data.items[0].match_type == "exact_email"
    assert result.data.next_cursor == "next-token"
    assert result.data.has_more is True


@pytest.mark.asyncio
async def test_search_contact_create_candidates_uses_create_search() -> None:
    service = _FakeContactSearchService(rows=[], next_cursor=None)

    result = await search_contact_create_candidates(
        response=FastAPIResponse(),
        q="sam@openai.com",
        limit=20,
        cursor=None,
        service=service,  # type: ignore[arg-type]
        _rl_read=None,
    )

    assert service.request == {
        "q": "sam@openai.com",
        "limit": 20,
        "cursor": None,
    }
    assert result.status == "SUCCESS"


@pytest.mark.asyncio
async def test_get_contact_detail_returns_page_ready_contract() -> None:
    org_id = uuid4()
    integration_id = uuid4()
    contact_id = uuid4()
    company_id = uuid4()
    owner_id = uuid4()
    deal_id = uuid4()
    participant_id = uuid4()
    now = datetime(2026, 6, 20, 15, 45, tzinfo=UTC)
    researched_at = datetime(2026, 6, 19, 8, 15, tzinfo=UTC)
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=integration_id,
        contact_id=contact_id,
        first_name="Vickesh",
        last_name="Shah",
        full_name="Vickesh Shah",
        email="vickesh@lemonedge.com",
        additional_emails=[],
        job_title="Head of Sales",
        phone="+1 415 555 0100",
        mobile_phone="+1 415 555 0101",
        linkedin_url="https://linkedin.com/in/vickesh",
        website="https://lemonedge.com",
        company_id=company_id,
        company_name="LemonEdge",
        owner_id=owner_id,
        owner_name="Nikhil Singhal",
        owner_email="nikhil@example.com",
        contact_research="Vickesh leads regional sales at LemonEdge.",
        last_researched_at=researched_at,
        profile_enrichment={"headline": "Sales leader"},
        profile_enrichment_source="apollo",
        profile_enriched_at=researched_at,
        created_at=now,
        updated_at=now,
        synced_at=now,
    )
    deal = CRMDeal(
        organisation_id=org_id,
        integration_id=integration_id,
        deal_id=deal_id,
        name="Vickesh-LemonEdge",
        deal_status=CRMDealStatus.OPEN,
        stage_name="Appointment Scheduled",
        last_activity_at=now,
    )
    participant = CRMDealParticipant(
        organisation_id=org_id,
        integration_id=integration_id,
        deal_id=deal_id,
        participant_id=participant_id,
        participant_type=CRMParticipantType.CONTACT,
        related_local_id=contact_id,
        relationship_role="buyer",
    )
    service = _FakeContactDetailService(
        {
            "contact": contact,
            "source": "hubspot",
            "company": {
                "id": company_id,
                "name": "LemonEdge",
                "domain": "lemonedge.com",
                "website": "https://lemonedge.com",
                "industry": "Financial Services",
                "linkedin_url": None,
                "country": None,
            },
            "owner": {
                "id": owner_id,
                "name": "Nikhil Singhal",
                "email": "nikhil@example.com",
            },
            "deals": [
                {
                    "deal": deal,
                    "participant": participant,
                }
            ],
        }
    )

    result = await get_contact_detail(
        contact_id=contact_id,
        response=FastAPIResponse(),
        service=service,  # type: ignore[arg-type]
        _rl_read=None,
    )

    dumped = result.data.model_dump()  # type: ignore[union-attr]
    assert dumped["company"] == {
        "id": company_id,
        "name": "LemonEdge",
        "domain": "lemonedge.com",
        "website": "https://lemonedge.com",
        "industry": "Financial Services",
        "linkedin_url": None,
        "country": None,
    }
    assert dumped["owner"] == {
        "id": owner_id,
        "name": "Nikhil Singhal",
        "email": "nikhil@example.com",
    }
    assert dumped["research_summary"] == {
        "summary": "Vickesh leads regional sales at LemonEdge.",
        "last_researched_at": researched_at,
    }
    assert dumped["profile_enrichment"] == {
        "is_enriched": True,
        "source": "apollo",
        "enriched_at": researched_at,
        "data": {"headline": "Sales leader"},
    }
    assert dumped["deal_involvement"] == [
        {
            "deal_id": deal_id,
            "deal_name": "Vickesh-LemonEdge",
            "deal_status": "open",
            "stage_name": "Appointment Scheduled",
            "last_activity_at": now,
            "participant_id": participant_id,
            "relationship_role": "buyer",
        }
    ]
    assert "stakeholder_summary" not in dumped
    assert "stance" not in dumped["deal_involvement"][0]
    assert "decision_power" not in dumped["deal_involvement"][0]
