from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.service.integrations.crm.records.salesforce.companies import company_writer
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
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=2)
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
    mapping_helper.upsert_integration_mapping.assert_not_awaited()
    mapping_helper.bulk_upsert_integration_mappings.assert_awaited_once()
    mapped_rows = mapping_helper.bulk_upsert_integration_mappings.await_args.args[0]
    assert [row.external_id for row in mapped_rows] == ["001A", "001C"]
    assert {row.external_object_type for row in mapped_rows} == {CRMSourceObjectType.COMPANY}
    assert {row.local_object_type for row in mapped_rows} == {CRMSourceObjectType.COMPANY}
    assert mapped_rows[0].local_object_id == existing_inactive.company_id
    assert mapped_rows[1].local_object_id == created_rows[0].company_id
    mapping_helper.mark_missing_deleted.assert_awaited_once()


@pytest.mark.asyncio
async def test_load_existing_rows_chunks_local_id_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    companies = [
        CRMCompany(
            company_id=uuid4(),
            organisation_id=organisation_id,
            integration_id=integration_id,
            name=f"Company {index}",
        )
        for index in range(3)
    ]

    monkeypatch.setattr(company_writer, "_LOCAL_ID_QUERY_CHUNK_SIZE", 2)

    scalars = [MagicMock(), MagicMock()]
    scalars[0].all.return_value = companies[:2]
    scalars[1].all.return_value = companies[2:]
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(scalars=lambda index=index: scalars[index]) for index in range(2)
        ]
    )

    writer = SalesforceCompanyWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.load_local_id_map = AsyncMock(
        return_value={f"001{index}": company.company_id for index, company in enumerate(companies)}
    )
    writer.source_mappings = mapping_helper

    rows = await writer._load_existing_rows()

    assert session.execute.await_count == 2
    assert rows == {f"001{index}": company for index, company in enumerate(companies)}
