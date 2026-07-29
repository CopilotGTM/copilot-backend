"""Regression coverage for HubSpot contact writer webhook interactions."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.service.integrations.crm.records.hubspot.contacts.contact_types import (
    NormalizedHubSpotContact,
)
from src.service.integrations.crm.records.hubspot.contacts.contact_writer import (
    HubSpotContactWriter,
)


def _normalized_contact(
    *,
    external_id: str,
    email: str = "buyer@example.com",
    source_modified_at: datetime | None = None,
) -> NormalizedHubSpotContact:
    return NormalizedHubSpotContact(
        external_id=external_id,
        first_name="Alex",
        last_name="Morgan",
        full_name="Alex Morgan",
        job_title="VP Sales",
        company_name="Acme",
        company_external_id=None,
        email=email,
        additional_emails=(),
        phone="123",
        mobile_phone="456",
        fax=None,
        website=None,
        owner_external_id=None,
        lifecycle_stage=None,
        lead_status=None,
        marketing_status=None,
        address="1 Main St",
        city="Pune",
        state="MH",
        postal_code="411001",
        country="India",
        timezone=None,
        last_contacted_at=None,
        last_activity_at=None,
        source_created_at=None,
        source_modified_at=source_modified_at,
        properties={"raw_properties": {}},
        data_payload={"id": external_id},
    )


def _mock_source_mappings(writer: HubSpotContactWriter) -> MagicMock:
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    writer.source_mappings = mapping_helper
    return mapping_helper


@pytest.mark.asyncio
async def test_apply_reactivates_soft_deleted_contact() -> None:
    """HubSpot upserts revive a local contact tombstone instead of inserting a duplicate."""
    organisation_id = uuid4()
    integration_id = uuid4()
    existing = CRMContact(
        contact_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="123",
        email="old@example.com",
        full_name="Deleted Contact",
        is_deleted=True,
    )
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    writer = HubSpotContactWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={"123": existing})  # type: ignore[method-assign]
    mapping_helper = _mock_source_mappings(writer)

    result = await writer.apply(
        normalized=[_normalized_contact(external_id="123", email="restored@example.com")],
        company_map={},
        owner_map={},
    )

    assert result.contacts_created == 0
    assert result.contacts_updated == 0
    assert result.contacts_reactivated == 1
    assert existing.is_deleted is False
    assert existing.email == "restored@example.com"
    assert existing.full_name == "Alex Morgan"
    mapping_helper.upsert_integration_mapping.assert_awaited_once()
    assert mapping_helper.upsert_integration_mapping.call_args.kwargs["external_object_type"] == (
        CRMSourceObjectType.CONTACT
    )
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_apply_reactivates_soft_deleted_contact_even_when_timestamp_matches() -> None:
    """Freshness skip must not block HubSpot contact reactivation."""
    organisation_id = uuid4()
    integration_id = uuid4()
    source_modified_at = datetime(2026, 5, 2, tzinfo=UTC)
    existing = CRMContact(
        contact_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="123",
        email="old@example.com",
        full_name="Deleted Contact",
        source_modified_at=source_modified_at,
        is_deleted=True,
    )
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    writer = HubSpotContactWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={"123": existing})  # type: ignore[method-assign]
    _mock_source_mappings(writer)

    result = await writer.apply(
        normalized=[
            replace(
                _normalized_contact(external_id="123", email="restored@example.com"),
                source_modified_at=source_modified_at,
            )
        ],
        company_map={},
        owner_map={},
    )

    assert result.contacts_reactivated == 1
    assert result.contacts_skipped_stale == 0
    assert existing.is_deleted is False
    assert existing.email == "restored@example.com"


def test_apply_updates_keeps_newer_projected_activity() -> None:
    """A provider replay cannot roll a Contact back behind Copilot activity."""
    organisation_id = uuid4()
    integration_id = uuid4()
    projected_at = datetime(2026, 7, 3, tzinfo=UTC)
    provider_at = datetime(2026, 7, 1, tzinfo=UTC)
    existing = CRMContact(
        contact_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        last_contacted_at=projected_at,
        last_activity_at=projected_at,
    )
    incoming = replace(
        _normalized_contact(external_id="123"),
        last_contacted_at=provider_at,
        last_activity_at=provider_at,
    )

    HubSpotContactWriter(
        session=AsyncMock(),
        organisation_id=organisation_id,
        integration_id=integration_id,
    )._apply_updates(existing, incoming, None, None, now=projected_at)

    assert existing.last_contacted_at == projected_at
    assert existing.last_activity_at == projected_at
