from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.service.integrations.crm.records.hubspot.users import user_sync_service
from src.service.integrations.crm.records.hubspot.users.user_sync_service import (
    HubSpotUserRecordSyncService,
)


class _ScalarRowsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


@pytest.mark.asyncio
async def test_load_owner_pk_map_uses_source_mapping_without_provider_row_filter(
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
            return {"owner-1": canonical_user_id}

    async def execute(stmt):
        captured["where"] = str(getattr(stmt, "whereclause", ""))
        return _ScalarRowsResult(
            [
                CRMUser(
                    user_id=canonical_user_id,
                    organisation_id=organisation_id,
                    integration_id=uuid4(),
                    email="owner@example.com",
                    is_deleted=False,
                )
            ]
        )

    monkeypatch.setattr(user_sync_service, "CRMSourceMappingHelper", FakeMappingHelper)

    result = await HubSpotUserRecordSyncService()._load_owner_pk_map(
        session=SimpleNamespace(execute=execute),
        organisation_id=organisation_id,
        integration_id=provider_integration_id,
    )

    assert result == {"owner-1": canonical_user_id}
    assert "crm_users.integration_id" not in captured["where"]
