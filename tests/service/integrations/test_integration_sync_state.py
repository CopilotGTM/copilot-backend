import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from src.domain.models.organisation_tables.sync import (
    IntegrationScope,
    IntegrationSyncState,
    IntegrationType,
    KnowledgeIntegration,
)
from src.service.integrations.capability_sync_state import (
    CapabilitySyncStateResolver,
)


def test_provider_parent_types_and_capability_child_unique_index() -> None:
    assert IntegrationType.GOOGLE.value == "google"
    assert IntegrationType.MICROSOFT.value == "microsoft"

    index_columns = {
        index.name: [column.name for column in index.columns]
        for index in IntegrationSyncState.__table__.indexes
    }

    assert index_columns["ux_integration_sync_states_integration_capability"] == [
        "integration_id",
        "capability",
    ]


def test_sync_state_follows_parent_integration_lifecycle() -> None:
    integration_id_column = IntegrationSyncState.__table__.c.integration_id

    foreign_key = next(iter(integration_id_column.foreign_keys))

    assert foreign_key.ondelete == "CASCADE"


def test_sync_state_migration_matches_timestamp_mixin() -> None:
    migration = _load_sync_state_migration()
    fake_op = _FakeMigrationOp()
    migration.op = fake_op

    migration.upgrade()

    table = fake_op.created_tables["integration_sync_states"]
    created_at = table.columns["created_at"]
    updated_at = table.columns["updated_at"]

    assert created_at.nullable is False
    assert updated_at.nullable is False
    assert created_at.type.timezone is True
    assert updated_at.type.timezone is True
    assert created_at.server_default is not None
    assert updated_at.server_default is not None
    assert table.foreign_keys["integration_id"].ondelete == "CASCADE"


@pytest.mark.asyncio
async def test_resolver_returns_detached_enabled_capability_snapshot() -> None:
    org_id = uuid4()
    integration_id = uuid4()
    parent = KnowledgeIntegration(
        integration_id=integration_id,
        organisation_id=org_id,
        created_by_user_id=uuid4(),
        integration_type=IntegrationType.GOOGLE,
        name="Google",
        integration_scope=IntegrationScope.USER,
        integration_email="sales@example.com",
        connection_id="google-connection",
        credentials={"refresh_token": "refresh"},
        scopes=["https://www.googleapis.com/auth/calendar.readonly"],
        connection_metadata={"provider_key": "google"},
        is_active=True,
    )
    state = IntegrationSyncState(
        organisation_id=org_id,
        integration_id=integration_id,
        capability=IntegrationType.GOOGLE_CALENDAR,
        cursor_metadata={"calendar_delta_link": "cursor-1"},
        webhook_metadata={"channel_id": "channel-1"},
        enabled=True,
    )

    resolver = CapabilitySyncStateResolver(_FakeSession([parent, state]))

    snapshot = await resolver.resolve(
        organisation_id=org_id,
        integration_id=integration_id,
        capability=IntegrationType.GOOGLE_CALENDAR,
    )

    assert snapshot.provider == IntegrationType.GOOGLE
    assert snapshot.capability == IntegrationType.GOOGLE_CALENDAR
    assert snapshot.connection_id == "google-connection"
    assert snapshot.credentials == {"refresh_token": "refresh"}
    assert snapshot.cursor_metadata == {"calendar_delta_link": "cursor-1"}
    assert snapshot.webhook_metadata == {"channel_id": "channel-1"}


@pytest.mark.asyncio
async def test_resolver_rejects_disabled_capability() -> None:
    org_id = uuid4()
    integration_id = uuid4()
    parent = KnowledgeIntegration(
        integration_id=integration_id,
        organisation_id=org_id,
        created_by_user_id=uuid4(),
        integration_type=IntegrationType.GOOGLE,
        name="Google",
        integration_scope=IntegrationScope.USER,
        connection_id="google-connection",
        credentials={},
        is_active=True,
    )
    disabled_state = IntegrationSyncState(
        organisation_id=org_id,
        integration_id=integration_id,
        capability=IntegrationType.GMAIL,
        enabled=False,
    )

    resolver = CapabilitySyncStateResolver(_FakeSession([parent, disabled_state]))

    with pytest.raises(ValueError, match="Capability is not enabled"):
        await resolver.resolve(
            organisation_id=org_id,
            integration_id=integration_id,
            capability=IntegrationType.GMAIL,
        )


class _FakeSession:
    def __init__(self, results: list[object]) -> None:
        self._results = list(results)

    async def execute(self, _statement: object) -> "_FakeResult":
        return _FakeResult(self._results.pop(0))


class _FakeResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


def _load_sync_state_migration() -> object:
    migration_path = (
        Path(__file__).parents[3]
        / "migrations"
        / "org_migrations"
        / "versions"
        / "3c7f79637744_055_provider_capability_sync_states.py"
    )
    spec = importlib.util.spec_from_file_location("provider_capability_sync_states", migration_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _CapturedTable:
    def __init__(self, elements: tuple[object, ...]) -> None:
        self.columns = {
            element.name: element for element in elements if isinstance(element, sa.Column)
        }
        self.foreign_keys = {
            element.column_keys[0]: element
            for element in elements
            if isinstance(element, sa.ForeignKeyConstraint)
        }


class _FakeMigrationOp:
    def __init__(self) -> None:
        self.created_tables: dict[str, _CapturedTable] = {}

    def create_table(self, name: str, *elements: object) -> None:
        self.created_tables[name] = _CapturedTable(elements)

    def create_index(self, *_args: object, **_kwargs: object) -> None:
        return None

    def f(self, name: str) -> str:
        return name

    def get_bind(self) -> "_FakeMigrationBind":
        return _FakeMigrationBind()


class _FakeMigrationBind:
    def execute(self, *_args: object, **_kwargs: object) -> "_FakeMigrationRows":
        return _FakeMigrationRows()


class _FakeMigrationRows:
    def mappings(self) -> "_FakeMigrationRows":
        return self

    def all(self) -> list[object]:
        return []
