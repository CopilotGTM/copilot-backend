from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync import (
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
)
from src.workers.integrations_worker.tasks.crm.salesforce.salesforce_user_record_sync_task import (
    _update_integration_metadata,
)


class FakeResult:
    def __init__(self, *, scalar: object | None = None) -> None:
        self._scalar = scalar

    def scalar_one_or_none(self) -> object | None:
        return self._scalar


def _make_session(*results: FakeResult) -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=list(results))
    session.commit = AsyncMock()
    session.add = MagicMock()
    return session


def _make_integration(**overrides: object) -> KnowledgeIntegration:
    defaults: dict[str, object] = {
        "organisation_id": uuid4(),
        "created_by_user_id": uuid4(),
        "integration_scope": IntegrationScope.ORGANIZATION,
        "integration_type": IntegrationType.SALESFORCE,
        "name": "Salesforce Custom Integration",
        "credentials": {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "instance_url": "https://acme.my.salesforce.com",
        },
        "connection_id": str(uuid4()),
        "is_active": True,
        "connection_metadata": {},
    }
    defaults.update(overrides)
    return KnowledgeIntegration(**defaults)


@pytest.mark.asyncio
async def test_update_integration_metadata_sets_salesforce_user_metadata() -> None:
    integration = _make_integration(connection_metadata={"provider_key": "salesforce"})
    session = _make_session(FakeResult(scalar=integration))

    await _update_integration_metadata(
        session=session,
        organisation_id=integration.organisation_id,
        integration_id=integration.integration_id,
        status="success",
        result_metadata={
            "fetched_users": 41,
            "users_created": 5,
            "users_updated": 10,
            "users_reactivated": 1,
            "users_deactivated": 2,
            "users_matched_public": 7,
            "users_minted_uuid": 8,
            "skipped_malformed": 0,
            "skipped_dup_email": 1,
            "app_user_index_size": 12,
            "deal_backfill_rows_updated": 9,
            "deal_backfill_per_op": {
                "owner_id": 7,
                "created_by_user_id": 1,
                "updated_by_user_id": 1,
            },
        },
        error_class=None,
        error_message=None,
    )

    users_metadata = integration.connection_metadata["record_sync"]["users"]
    assert users_metadata["status"] == "success"
    assert users_metadata["fetched_users"] == 41
    assert users_metadata["users_matched_public"] == 7
    assert users_metadata["deal_backfill_rows_updated"] == 9
    assert users_metadata["last_synced_at"].endswith("+00:00")
    assert integration.connection_metadata.get("initial_sync_status") != "success"
    assert isinstance(integration.last_synced_at, datetime)
    assert integration.last_synced_at.tzinfo is None
    session.commit.assert_awaited_once()
    session.add.assert_called_once_with(integration)
