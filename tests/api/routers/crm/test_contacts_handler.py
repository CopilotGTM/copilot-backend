from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import Response as FastAPIResponse
from src.api.routers.crm.contacts_handler import list_contacts
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact


class _FakeContactReadService:
    def __init__(self, rows: list[dict], total_count: int):
        self.rows = rows
        self.total_count = total_count

    async def list_contacts(self, **kwargs):
        self.request = kwargs
        return self.rows, self.total_count


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
