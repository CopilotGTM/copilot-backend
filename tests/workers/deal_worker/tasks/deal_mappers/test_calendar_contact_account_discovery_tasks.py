from __future__ import annotations

import inspect
from uuid import UUID

import pytest
from src.domain.models.organisation_tables.job_models import DealJobType
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.crm.discovery.calendar_contact_account_discovery_jobs import (
    prepare_calendar_discovery_job,
)
from src.workers.deal_worker.config import DealWorkerConfig
from src.workers.deal_worker.tasks.deal_mappers.calendar_contact_account_discovery_tasks import (
    CALENDAR_DISCOVERY_METADATA_KEY,
    _resolve_or_create_job,
    discover_calendar_contacts_accounts_task,
    should_run_calendar_discovery,
)
from src.workers.integrations_worker.tasks.calendar import org_task

ORG_ID = UUID("11111111-1111-1111-1111-111111111111")
INTEGRATION_ID = UUID("22222222-2222-2222-2222-222222222222")


def _integration(metadata: dict | None = None) -> KnowledgeIntegration:
    return KnowledgeIntegration(
        integration_id=INTEGRATION_ID,
        organisation_id=ORG_ID,
        created_by_user_id=UUID("33333333-3333-3333-3333-333333333333"),
        integration_type=IntegrationType.GOOGLE_CALENDAR,
        name="Google Calendar",
        credentials={},
        connection_id="conn",
        connection_metadata=metadata or {},
    )


class _ScalarResult:
    def __init__(self, value: object | None) -> None:
        self._value = value

    def scalars(self) -> _ScalarResult:
        return self

    def first(self) -> object | None:
        return self._value


class _PrepareJobSession:
    def __init__(self, integration: KnowledgeIntegration) -> None:
        self.integration = integration
        self.added: list[object] = []

    async def execute(self, _stmt: object) -> _ScalarResult:
        return _ScalarResult(self.integration)

    def add(self, item: object) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        return None


def test_calendar_discovery_runs_after_initial_sync_with_persisted_events() -> None:
    integration = _integration()

    should_run = should_run_calendar_discovery(
        integration=integration,
        is_initial=True,
        records_synced=3,
    )

    assert should_run is True
    assert CALENDAR_DISCOVERY_METADATA_KEY not in integration.connection_metadata


def test_calendar_discovery_does_not_run_for_incremental_or_empty_sync() -> None:
    for is_initial, records_synced in [(False, 3), (True, 0)]:
        integration = _integration()

        should_run = should_run_calendar_discovery(
            integration=integration,
            is_initial=is_initial,
            records_synced=records_synced,
        )

        assert should_run is False
        assert CALENDAR_DISCOVERY_METADATA_KEY not in integration.connection_metadata


def test_calendar_discovery_metadata_prevents_duplicate_run() -> None:
    for status in ("success", "pending", "in_progress"):
        metadata = {"status": status}
        if status in {"pending", "in_progress"}:
            metadata["job_id"] = "44444444-4444-4444-4444-444444444444"
        integration = _integration({CALENDAR_DISCOVERY_METADATA_KEY: metadata})

        should_run = should_run_calendar_discovery(
            integration=integration,
            is_initial=True,
            records_synced=3,
        )

        assert should_run is False


def test_calendar_discovery_incomplete_pending_metadata_can_be_recreated() -> None:
    integration = _integration({CALENDAR_DISCOVERY_METADATA_KEY: {"status": "pending"}})

    should_run = should_run_calendar_discovery(
        integration=integration,
        is_initial=True,
        records_synced=3,
    )

    assert should_run is True


def test_calendar_discovery_metadata_allows_failed_rerun() -> None:
    integration = _integration({CALENDAR_DISCOVERY_METADATA_KEY: {"status": "failed"}})

    should_run = should_run_calendar_discovery(
        integration=integration,
        is_initial=True,
        records_synced=3,
    )

    assert should_run is True


def test_calendar_connection_discovery_runs_after_successful_empty_reconnect() -> None:
    integration = _integration(
        {
            CALENDAR_DISCOVERY_METADATA_KEY: {
                "status": "success",
                "trigger": "calendar_connection",
                "trigger_id": "previous-connection",
                "job_id": "44444444-4444-4444-4444-444444444444",
            }
        }
    )

    should_run = should_run_calendar_discovery(
        integration=integration,
        is_initial=False,
        records_synced=0,
        trigger="calendar_connection",
        trigger_id="new-connection",
    )

    assert should_run is True


def test_calendar_connection_discovery_skips_prior_success_for_same_trigger() -> None:
    integration = _integration(
        {
            CALENDAR_DISCOVERY_METADATA_KEY: {
                "status": "success",
                "trigger": "calendar_connection",
                "trigger_id": "same-connection",
                "job_id": "44444444-4444-4444-4444-444444444444",
            }
        }
    )

    should_run = should_run_calendar_discovery(
        integration=integration,
        is_initial=False,
        records_synced=0,
        trigger="calendar_connection",
        trigger_id="same-connection",
    )

    assert should_run is False


@pytest.mark.asyncio
async def test_calendar_connection_discovery_requires_successful_sync_result() -> None:
    job_id = await _resolve_or_create_job(
        org_id=ORG_ID,
        job_id=None,
        source_integration_id=str(INTEGRATION_ID),
        sync_result={"status": "failed", "events_synced": 0},
        trigger="calendar_connection",
        trigger_id="new-connection",
    )

    assert job_id is None


@pytest.mark.asyncio
async def test_prepare_calendar_discovery_job_records_connection_trigger_for_empty_sync() -> None:
    integration = _integration(
        {
            CALENDAR_DISCOVERY_METADATA_KEY: {
                "status": "success",
                "trigger": "calendar_connection",
                "trigger_id": "previous-connection",
                "job_id": "44444444-4444-4444-4444-444444444444",
            }
        }
    )
    session = _PrepareJobSession(integration)

    dispatch = await prepare_calendar_discovery_job(
        session=session,
        organisation_id=ORG_ID,
        source_integration_id=INTEGRATION_ID,
        is_initial=False,
        records_synced=0,
        trigger="calendar_connection",
        trigger_id="new-connection",
    )

    assert dispatch is not None
    job = session.added[0]
    assert job.job_metadata["trigger"] == "calendar_connection"
    assert job.job_metadata["trigger_id"] == "new-connection"
    assert job.job_metadata["records_synced"] == 0
    metadata = integration.connection_metadata[CALENDAR_DISCOVERY_METADATA_KEY]
    assert metadata["status"] == "pending"
    assert metadata["trigger"] == "calendar_connection"
    assert metadata["trigger_id"] == "new-connection"


@pytest.mark.asyncio
async def test_prepare_calendar_discovery_job_blocks_duplicate_connection_trigger() -> None:
    integration = _integration(
        {
            CALENDAR_DISCOVERY_METADATA_KEY: {
                "status": "pending",
                "trigger": "calendar_connection",
                "trigger_id": "same-connection",
                "job_id": "44444444-4444-4444-4444-444444444444",
            }
        }
    )
    session = _PrepareJobSession(integration)

    dispatch = await prepare_calendar_discovery_job(
        session=session,
        organisation_id=ORG_ID,
        source_integration_id=INTEGRATION_ID,
        is_initial=False,
        records_synced=0,
        trigger="calendar_connection",
        trigger_id="same-connection",
    )

    assert dispatch is None


def test_calendar_discovery_has_internal_deal_job_type() -> None:
    assert DealJobType.CALENDAR_CONTACT_ACCOUNT_DISCOVERY.value == (
        "calendar_contact_account_discovery"
    )


def test_calendar_discovery_task_requires_job_id() -> None:
    signature = inspect.signature(discover_calendar_contacts_accounts_task)

    assert list(signature.parameters) == [
        "sync_result",
        "organisation_id",
        "source_integration_id",
        "job_id",
        "trigger",
        "trigger_id",
    ]


def test_calendar_discovery_task_is_owned_by_deal_worker_queue() -> None:
    assert discover_calendar_contacts_accounts_task.name == (
        "deal.discover_calendar_contacts_accounts"
    )

    route = DealWorkerConfig().task_routes["deal.discover_calendar_contacts_accounts"]

    assert route["queue"] == "deal"
    assert route["routing_key"] == "deal"


def test_calendar_discovery_is_not_wired_from_org_sync_task() -> None:
    names = org_task.sync_calendar_org_task.run.__code__.co_names

    assert "prepare_calendar_discovery_job" not in names
    assert "deal.discover_calendar_contacts_accounts" not in org_task.__dict__.values()
