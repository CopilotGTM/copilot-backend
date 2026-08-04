"""Microsoft parent regression coverage for manual and scheduled Teams sync."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from src.domain.models.organisation_tables.sync import (
    IntegrationScope,
    IntegrationSyncState,
    IntegrationType,
    KnowledgeIntegration,
)
from src.workers.integrations_worker.tasks.messaging import (
    channels_integration_task,
    channels_org_task,
)


def _microsoft_parent(*, last_synced_at: datetime | None = None) -> KnowledgeIntegration:
    return KnowledgeIntegration(
        integration_id=uuid4(),
        organisation_id=uuid4(),
        created_by_user_id=uuid4(),
        integration_type=IntegrationType.MICROSOFT,
        integration_scope=IntegrationScope.USER,
        name="Microsoft",
        connection_id="microsoft-connection",
        credentials={"refresh_token": "refresh"},
        scopes=["Channel.ReadBasic.All", "Chat.Read"],
        is_active=True,
        last_synced_at=last_synced_at,
    )


def _teams_state(
    parent: KnowledgeIntegration,
    *,
    enabled: bool,
    last_synced_at: datetime | None = None,
) -> IntegrationSyncState:
    return IntegrationSyncState(
        organisation_id=parent.organisation_id,
        integration_id=parent.integration_id,
        capability=IntegrationType.TEAMS,
        enabled=enabled,
        last_synced_at=last_synced_at,
    )


class _FakeResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        """Return the single ORM value requested by worker queries."""
        return self.value

    def scalars(self) -> _FakeResult:
        """Mirror SQLAlchemy's scalar-result chaining for discovery queries."""
        return self

    def all(self) -> list[object]:
        """Return every fake integration selected during scheduled discovery."""
        return list(self.value)  # type: ignore[arg-type]


class _FakeSession:
    def __init__(self, results: list[object]) -> None:
        self.results = list(results)
        self.committed = False

    async def execute(self, _statement: object) -> _FakeResult:
        """Return query results in the order the worker's phases request them."""
        return _FakeResult(self.results.pop(0))

    def add(self, _record: object) -> None:
        """Accept ORM writes while tests inspect the same in-memory model instance."""
        return None

    async def commit(self) -> None:
        """Record that capability state reached the worker's commit boundary."""
        self.committed = True


def _session_provider(sessions: list[_FakeSession]):
    remaining = list(sessions)

    async def acquire(_organisation_id):
        """Yield one fresh fake session for each worker persistence phase."""
        yield remaining.pop(0)

    return acquire


def _stub_provider_calls(monkeypatch, module):
    fetch_channels = AsyncMock(return_value=[])
    fetch_chats = AsyncMock(return_value=SimpleNamespace(chats=[]))
    monkeypatch.setattr(module, "teams_fetch_channels", fetch_channels)
    monkeypatch.setattr(module, "teams_fetch_chats", fetch_chats)
    return fetch_channels, fetch_chats


def test_manual_microsoft_sync_uses_and_persists_enabled_teams_state(monkeypatch) -> None:
    """The Teams child timestamp, not the shared parent timestamp, owns incremental sync."""
    parent_timestamp = datetime(2026, 7, 1, tzinfo=UTC)
    teams_timestamp = datetime(2026, 8, 1, tzinfo=UTC)
    parent = _microsoft_parent(last_synced_at=parent_timestamp)
    state = _teams_state(parent, enabled=True, last_synced_at=teams_timestamp)
    state.last_sync_status = "provider_error"
    state.last_sync_error = "temporary Graph failure"
    state.consecutive_sync_failures = 2
    phase_one = _FakeSession([parent, parent, state])
    history = _FakeSession([])
    persistence = _FakeSession([parent, state])
    monkeypatch.setattr(
        channels_integration_task,
        "acquire_org_async_session",
        _session_provider([phase_one, history, persistence]),
    )
    monkeypatch.setattr(
        channels_integration_task.SyncHistoryManager,
        "start_fresh",
        AsyncMock(return_value=None),
    )
    fetch_channels, fetch_chats = _stub_provider_calls(monkeypatch, channels_integration_task)

    result = channels_integration_task.sync_messaging_channels_integration_task.run(
        organisation_id=str(parent.organisation_id),
        integration_id=str(parent.integration_id),
    )

    assert result["status"] == "success"
    assert fetch_channels.await_args.kwargs["last_synced_at"] == teams_timestamp
    assert fetch_chats.await_args.kwargs["last_synced_at"] == teams_timestamp
    assert state.last_synced_at is not None
    assert state.last_synced_at != teams_timestamp
    assert state.last_sync_status == "success"
    assert state.last_sync_error is None
    assert state.consecutive_sync_failures == 0
    assert parent.last_synced_at == parent_timestamp
    assert persistence.committed is True


def test_manual_microsoft_sync_rejects_disabled_teams_state(monkeypatch) -> None:
    """A Microsoft token must not grant Teams runtime access when Teams is disabled."""
    parent = _microsoft_parent()
    state = _teams_state(parent, enabled=False)
    monkeypatch.setattr(
        channels_integration_task,
        "acquire_org_async_session",
        _session_provider([_FakeSession([parent, parent, state])]),
    )
    fetch_channels, fetch_chats = _stub_provider_calls(monkeypatch, channels_integration_task)

    result = channels_integration_task.sync_messaging_channels_integration_task.run(
        organisation_id=str(parent.organisation_id),
        integration_id=str(parent.integration_id),
    )

    assert result["status"] == "failed"
    assert result["error"] == "Capability is not enabled"
    fetch_channels.assert_not_awaited()
    fetch_chats.assert_not_awaited()


def test_scheduled_sync_includes_only_enabled_microsoft_teams_state(monkeypatch) -> None:
    """Scheduled discovery skips disabled Teams children and persists the enabled child state."""
    enabled_parent = _microsoft_parent()
    enabled_state = _teams_state(enabled_parent, enabled=True)
    enabled_state.last_sync_status = "provider_error"
    enabled_state.last_sync_error = "temporary Graph failure"
    enabled_state.consecutive_sync_failures = 2
    disabled_parent = _microsoft_parent()
    disabled_parent.organisation_id = enabled_parent.organisation_id
    disabled_state = _teams_state(disabled_parent, enabled=False)
    discovery = _FakeSession(
        [
            [enabled_parent, disabled_parent],
            enabled_parent,
            enabled_state,
            disabled_parent,
            disabled_state,
        ]
    )
    history = _FakeSession([])
    persistence = _FakeSession([enabled_parent, enabled_state])
    monkeypatch.setattr(
        channels_org_task,
        "acquire_org_async_session",
        _session_provider([discovery, history, persistence]),
    )
    monkeypatch.setattr(
        channels_org_task.SyncHistoryManager,
        "start_fresh",
        AsyncMock(return_value=None),
    )
    record_parent_success = AsyncMock()
    monkeypatch.setattr(channels_org_task, "record_sync_success", record_parent_success)
    fetch_channels, fetch_chats = _stub_provider_calls(monkeypatch, channels_org_task)

    result = channels_org_task.sync_messaging_channels_org_task.run(
        organisation_id=str(enabled_parent.organisation_id)
    )

    assert result["status"] == "completed"
    assert result["summary"]["total_integrations"] == 1
    assert fetch_channels.await_count == 1
    assert fetch_chats.await_count == 1
    assert enabled_state.last_synced_at is not None
    assert enabled_state.last_sync_status == "success"
    assert enabled_state.last_sync_error is None
    assert enabled_state.consecutive_sync_failures == 0
    assert disabled_state.last_synced_at is None
    assert enabled_parent.last_synced_at is None
    assert persistence.committed is True
    record_parent_success.assert_not_awaited()
