from __future__ import annotations

from uuid import uuid4

import pytest
from src.service.integrations.crm.records.salesforce.companies.company_sync_service import (
    SalesforceCompanyRecordSyncService,
)
from src.service.integrations.crm.records.salesforce.companies.company_types import (
    CompanyWriteResult,
)


@pytest.mark.asyncio
async def test_persist_single_account_reuses_company_writer_without_snapshot_deactivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SalesforceCompanyRecordSyncService()
    captured: dict[str, object] = {}

    async def fake_persist(
        *,
        organisation_id,
        integration_id,
        normalized,
        deactivate_missing,
    ):
        captured["normalized"] = normalized
        captured["deactivate_missing"] = deactivate_missing
        return (
            CompanyWriteResult(companies_created=1),
            {"rows_updated": 2, "per_op": {"company_external_id": 2}},
        )

    monkeypatch.setattr(service, "_persist", fake_persist)

    result = await service.persist_single_account(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        raw_account={"Id": "001xx0000000001AAA", "Name": "Acme"},
    )

    assert result["companies_created"] == 1
    assert result["deal_backfill_rows_updated"] == 2
    assert captured["deactivate_missing"] is False
