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
from src.workers.integrations_worker.tasks.crm.salesforce.salesforce_deal_record_sync_task import (
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
async def test_update_integration_metadata_sets_salesforce_deal_metadata() -> None:
    integration = _make_integration(connection_metadata={"provider_key": "salesforce"})
    session = _make_session(FakeResult(scalar=integration))

    await _update_integration_metadata(
        session=session,
        organisation_id=integration.organisation_id,
        integration_id=integration.integration_id,
        status="success",
        result_metadata={
            "fetched_opportunities": 12,
            "property_list_size": 40,
            "fetched_stage_history_rows": 20,
            "fetched_participant_rows": 6,
            "deals_created": 4,
            "deals_updated": 8,
            "unresolved_pipeline_link": 0,
            "unresolved_stage_link": 1,
            "unresolved_owner_link": 0,
            "unresolved_company_link": 1,
            "unresolved_contact_link": 0,
            "unresolved_created_by_link": 0,
            "unresolved_updated_by_link": 0,
            "skipped_malformed_opportunities": 0,
            "skipped_malformed_stage_history": 0,
            "skipped_malformed_participants": 0,
            "stage_history_created": 20,
            "stage_history_updated": 0,
            "stage_history_unresolved_stage": 1,
            "stage_history_unresolved_from_stage": 0,
            "participants_created": 8,
            "participants_updated": 0,
            "participants_unresolved_related": 1,
            "associations_created": 8,
            "associations_updated": 0,
            "associations_unresolved_to_local": 1,
            "deal_backfill_rows_updated": 2,
            "deal_backfill_per_op": {"company_id": 1, "primary_contact_id": 1},
        },
        error_class=None,
        error_message=None,
    )

    deals_metadata = integration.connection_metadata["record_sync"]["deals"]
    assert deals_metadata["status"] == "success"
    assert deals_metadata["fetched_opportunities"] == 12
    assert deals_metadata["fetched_stage_history_rows"] == 20
    assert deals_metadata["participants_created"] == 8
    assert deals_metadata["deal_backfill_rows_updated"] == 2
    assert deals_metadata["last_synced_at"].endswith("+00:00")
    assert integration.connection_metadata.get("initial_sync_status") != "success"
    assert isinstance(integration.last_synced_at, datetime)
    assert integration.last_synced_at.tzinfo is None
    session.commit.assert_awaited_once()
    session.add.assert_called_once_with(integration)
