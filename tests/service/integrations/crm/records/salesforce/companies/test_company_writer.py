from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.service.integrations.crm.records.salesforce.companies.company_types import (
    NormalizedSalesforceCompany,
)
from src.service.integrations.crm.records.salesforce.companies.company_writer import (
    SalesforceCompanyWriter,
)


def _normalized_company(
    *,
    external_id: str,
    owner_external_id: str | None,
) -> NormalizedSalesforceCompany:
    return NormalizedSalesforceCompany(
        external_id=external_id,
        name=f"Company {external_id}",
        domain=f"{external_id.lower()}.example.com",
        website=f"https://{external_id.lower()}.example.com",
        description="Key account",
        industry="Software",
        company_type="Customer",
        lifecycle_stage=None,
        owner_external_id=owner_external_id,
        number_of_employees=25,
        annual_revenue=1000.0,
        phone="123",
        city="Pune",
        state="MH",
        country="India",
        address="1 Main St",
        postal_code="411001",
        timezone=None,
        linkedin_url=None,
        source_created_at=None,
        source_modified_at=None,
        properties={"salesforce_object": "Account", "raw_fields": {}},
        data_payload={"Id": external_id},
    )


@pytest.mark.asyncio
async def test_writer_reactivates_present_rows_creates_new_rows_and_deactivates_missing_rows() -> (
    None
):
    organisation_id = uuid4()
    integration_id = uuid4()
    owner_id = uuid4()

    existing_inactive = CRMCompany(
        company_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="001A",
        name="Old A",
        is_active=False,
    )
    stale_missing = CRMCompany(
        company_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="001B",
        name="Stale B",
        is_active=True,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = SalesforceCompanyWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.mark_missing_deleted = AsyncMock()
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(  # type: ignore[method-assign]
        return_value={"001A": existing_inactive, "001B": stale_missing}
    )

    result = await writer.apply(
        normalized=[
            _normalized_company(external_id="001A", owner_external_id="005MISSING"),
            _normalized_company(external_id="001C", owner_external_id="005A"),
        ],
        all_external_ids=["001A", "001C"],
        owner_map={"005A": owner_id},
    )

    assert result.companies_created == 1
    assert result.companies_reactivated == 1
    assert result.companies_updated == 0
    assert result.companies_deactivated == 1
    assert result.unresolved_owner_link == 1
    assert existing_inactive.is_active is True
    assert stale_missing.is_active is False
    created_rows = [call.args[0] for call in session.add.call_args_list]
    assert len(created_rows) == 1
    assert not hasattr(created_rows[0], "external_id")
    assert created_rows[0].owner_id == owner_id
    mapped_external_ids = [
        call.kwargs["external_id"]
        for call in mapping_helper.upsert_integration_mapping.call_args_list
    ]
    assert mapped_external_ids == ["001A", "001C"]
    assert (
        mapping_helper.upsert_integration_mapping.call_args_list[0].kwargs["external_object_type"]
        == CRMSourceObjectType.COMPANY
    )
    mapping_helper.mark_missing_deleted.assert_awaited_once()
