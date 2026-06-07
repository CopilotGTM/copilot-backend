from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.service.integrations.crm.records.salesforce.contacts import contact_writer
from src.service.integrations.crm.records.salesforce.contacts.contact_types import (
    NormalizedSalesforceContact,
)
from src.service.integrations.crm.records.salesforce.contacts.contact_writer import (
    SalesforceContactWriter,
)


def _normalized_contact(
    *,
    external_id: str,
    company_external_id: str | None,
    owner_external_id: str | None,
) -> NormalizedSalesforceContact:
    return NormalizedSalesforceContact(
        external_id=external_id,
        first_name="Alex",
        last_name="Morgan",
        full_name="Alex Morgan",
        job_title="VP Sales",
        company_name=None,
        company_external_id=company_external_id,
        email=f"{external_id.lower()}@example.com",
        additional_emails=(),
        phone="123",
        mobile_phone="456",
        fax=None,
        website=None,
        owner_external_id=owner_external_id,
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
        source_modified_at=None,
        properties={"salesforce_object": "Contact", "raw_fields": {}},
        data_payload={"Id": external_id},
    )


@pytest.mark.asyncio
async def test_writer_creates_and_updates_contacts_with_write_time_fk_resolution() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    company_id = uuid4()
    owner_id = uuid4()

    existing = CRMContact(
        contact_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="003A",
        email="old@example.com",
        full_name="Old Name",
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = SalesforceContactWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=2)
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(return_value={"003A": existing})  # type: ignore[method-assign]

    result = await writer.apply(
        normalized=[
            _normalized_contact(
                external_id="003A",
                company_external_id="001MISSING",
                owner_external_id="005MISSING",
            ),
            _normalized_contact(
                external_id="003B",
                company_external_id="001A",
                owner_external_id="005A",
            ),
        ],
        company_map={"001A": company_id},
        owner_map={"005A": owner_id},
    )

    assert result.contacts_created == 1
    assert result.contacts_updated == 1
    assert result.unresolved_company_link == 1
    assert result.unresolved_owner_link == 1
    assert existing.company_id is None
    assert existing.owner_id is None
    created_rows = [call.args[0] for call in session.add.call_args_list]
    assert len(created_rows) == 1
    assert not hasattr(created_rows[0], "external_id")
    assert created_rows[0].company_id == company_id
    assert created_rows[0].owner_id == owner_id
    mapping_helper.upsert_integration_mapping.assert_not_awaited()
    mapping_helper.bulk_upsert_integration_mappings.assert_awaited_once()
    mapped_rows = mapping_helper.bulk_upsert_integration_mappings.await_args.args[0]
    mapped_external_ids = [row.external_id for row in mapped_rows]
    assert mapped_external_ids == ["003A", "003B"]
    assert mapped_rows[0].external_object_type == CRMSourceObjectType.CONTACT
    assert mapped_rows[0].local_object_type == CRMSourceObjectType.CONTACT
    assert mapped_rows[0].local_object_id == existing.contact_id
    assert mapped_rows[1].local_object_id == created_rows[0].contact_id


@pytest.mark.asyncio
async def test_writer_reactivates_soft_deleted_contact_without_inserting_duplicate() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    contact_id = uuid4()

    existing = CRMContact(
        contact_id=contact_id,
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="003A",
        email="old@example.com",
        full_name="Old Name",
        is_deleted=True,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = SalesforceContactWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=1)
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(return_value={"003A": existing})  # type: ignore[method-assign]

    result = await writer.apply(
        normalized=[
            _normalized_contact(
                external_id="003A",
                company_external_id=None,
                owner_external_id=None,
            ),
        ],
        company_map={},
        owner_map={},
    )

    assert result.contacts_created == 0
    assert result.contacts_updated == 0
    assert result.contacts_reactivated == 1
    assert existing.contact_id == contact_id
    assert existing.is_deleted is False
    assert existing.full_name == "Alex Morgan"
    assert existing.email == "003a@example.com"
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_load_existing_rows_includes_soft_deleted_contacts() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    existing = CRMContact(
        contact_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="003A",
        is_deleted=True,
    )

    scalars = MagicMock()
    scalars.all.return_value = [existing]
    session = AsyncMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda: scalars))

    writer = SalesforceContactWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.load_local_id_map = AsyncMock(return_value={"003A": existing.contact_id})
    writer.source_mappings = mapping_helper

    rows = await writer._load_existing_rows()

    statement = session.execute.call_args.args[0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "crm_contacts.is_deleted = false" not in compiled
    assert rows == {"003A": existing}
    mapping_helper.load_local_id_map.assert_awaited_once_with(
        object_type=CRMSourceObjectType.CONTACT,
        external_ids=None,
        include_deleted=True,
    )


@pytest.mark.asyncio
async def test_load_existing_rows_chunks_local_id_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    contacts = [
        CRMContact(
            contact_id=uuid4(),
            organisation_id=organisation_id,
            integration_id=integration_id,
            email=f"contact{index}@example.com",
        )
        for index in range(3)
    ]

    monkeypatch.setattr(contact_writer, "_LOCAL_ID_QUERY_CHUNK_SIZE", 2)

    scalars = [MagicMock(), MagicMock()]
    scalars[0].all.return_value = contacts[:2]
    scalars[1].all.return_value = contacts[2:]
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(scalars=lambda index=index: scalars[index]) for index in range(2)
        ]
    )

    writer = SalesforceContactWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.load_local_id_map = AsyncMock(
        return_value={f"003{index}": contact.contact_id for index, contact in enumerate(contacts)}
    )
    writer.source_mappings = mapping_helper

    rows = await writer._load_existing_rows()

    assert session.execute.await_count == 2
    assert rows == {f"003{index}": contact for index, contact in enumerate(contacts)}
