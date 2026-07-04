from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.service.integrations.crm.records.salesforce.users import user_sync_service
from src.service.integrations.crm.records.salesforce.users.user_sync_service import (
    SalesforceUserRecordSyncService,
)


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


@pytest.mark.asyncio
async def test_load_user_pk_map_uses_source_mapping_without_provider_row_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organisation_id = uuid4()
    provider_integration_id = uuid4()
    canonical_user_id = uuid4()
    captured = {}

    class FakeMappingHelper:
        def __init__(self, **_kwargs):
            pass

        async def load_local_id_map(self, **_kwargs):
            return {"005A": canonical_user_id}

    async def execute(stmt):
        captured["where"] = str(getattr(stmt, "whereclause", ""))
        return _RowsResult([(canonical_user_id,)])

    monkeypatch.setattr(user_sync_service, "CRMSourceMappingHelper", FakeMappingHelper)

    result = await SalesforceUserRecordSyncService()._load_user_pk_map(
        session=SimpleNamespace(execute=execute),
        organisation_id=organisation_id,
        integration_id=provider_integration_id,
    )

    assert result == {"005A": canonical_user_id}
    assert "crm_users.integration_id" not in captured["where"]
