"""Safe structured-telemetry contracts for the shared-row tenant boundary."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from src.infra.database import provider_async, provider_sync, tenant_runtime_readiness
from src.infra.database.provider_async import AsyncSqlAlchemyProvider
from src.infra.database.provider_sync import SyncSqlAlchemyProvider
from src.infra.database.tenant_search_path import parse_tenant_organisation_id

from scripts.maintenance import (
    cutover_shared_tenant_schema,
    manage_shared_tenant_data,
)


def _event_records(caplog, event: str) -> list[logging.LogRecord]:
    """Return only structured records belonging to one stable alert event."""

    return [record for record in caplog.records if getattr(record, "event", None) == event]


def test_malformed_tenant_context_emits_safe_structured_failure(caplog) -> None:
    """Invalid external identity must alert without echoing the untrusted value."""

    unsafe_value = "password=do-not-log"
    with caplog.at_level(logging.ERROR), pytest.raises(ValueError):
        parse_tenant_organisation_id(unsafe_value)

    records = _event_records(caplog, "shared_tenancy_context")
    assert len(records) == 1
    record = records[0]
    assert record.operation == "parse_organisation_id"
    assert record.outcome == "failed"
    assert record.error_type == "ValueError"
    assert record.duration_ms >= 0
    assert unsafe_value not in caplog.text


class _CatalogResult:
    def __init__(self, findings: list[str]) -> None:
        self._findings = findings

    def scalars(self) -> list[str]:
        """Return the injected catalog findings."""

        return self._findings


class _CatalogSession:
    def __init__(self, findings: list[str]) -> None:
        self._findings = findings

    def execute(self, _statement, _parameters=None) -> _CatalogResult:
        """Return deterministic readiness findings without a live database."""

        return _CatalogResult(self._findings)


class _UnavailableCatalogSession:
    def execute(self, _statement, _parameters=None):
        """Simulate a catalog query failure whose payload must stay private."""

        raise RuntimeError("connection string=do-not-log")


@pytest.mark.parametrize(
    ("findings", "outcome", "level"),
    [
        ([], "passed", logging.INFO),
        (["table deals: RLS disabled"], "failed", logging.ERROR),
    ],
)
def test_runtime_catalog_gate_emits_duration_and_outcome(
    caplog,
    findings: list[str],
    outcome: str,
    level: int,
) -> None:
    """Startup readiness must expose a stable pass/fail alert contract."""

    with caplog.at_level(level):
        if findings:
            with pytest.raises(tenant_runtime_readiness.TenantRuntimeReadinessError):
                tenant_runtime_readiness.validate_tenant_runtime_catalog(
                    _CatalogSession(findings),
                    runtime_role="tenant_runtime",
                    set_read_only=False,
                )
        else:
            tenant_runtime_readiness.validate_tenant_runtime_catalog(
                _CatalogSession(findings),
                runtime_role="tenant_runtime",
                set_read_only=False,
            )

    records = _event_records(caplog, "shared_tenancy_startup_gate")
    assert len(records) == 1
    record = records[0]
    assert record.operation == "validate_runtime_catalog"
    assert record.outcome == outcome
    assert record.schema == "tenant_data"
    assert record.runtime_role == "tenant_runtime"
    assert record.finding_count == len(findings)
    assert record.duration_ms >= 0


def test_runtime_catalog_query_failure_emits_safe_startup_signal(caplog) -> None:
    """A catalog outage must emit the same startup alert without its payload."""

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        tenant_runtime_readiness.validate_tenant_runtime_catalog(
            _UnavailableCatalogSession(),
            runtime_role="tenant_runtime",
            set_read_only=False,
        )

    records = _event_records(caplog, "shared_tenancy_startup_gate")
    assert len(records) == 1
    record = records[0]
    assert record.outcome == "failed"
    assert record.finding_count == -1
    assert record.error_type == "RuntimeError"
    assert "do-not-log" not in caplog.text


class _FakeSyncSession:
    def __init__(self) -> None:
        self.info = {}
        self.rolled_back = False
        self.closed = False

    def commit(self) -> None:
        """Mirror a successful session commit."""

    def rollback(self) -> None:
        """Record rollback after the injected failure."""

        self.rolled_back = True

    def close(self) -> None:
        """Record deterministic session cleanup."""

        self.closed = True


class _FakeAsyncSession:
    def __init__(self) -> None:
        self.sync_session = _FakeSyncSession()
        self.rolled_back = False
        self.closed = False

    async def rollback(self) -> None:
        """Record rollback after the injected async failure."""

        self.rolled_back = True

    async def close(self) -> None:
        """Record deterministic async session cleanup."""

        self.closed = True


class _RlsViolationError(RuntimeError):
    sqlstate = "42501"


@pytest.mark.parametrize(
    ("error", "failure_kind", "sqlstate"),
    [
        (_RlsViolationError("password=do-not-log"), "rls_violation", "42501"),
        (
            SQLAlchemyTimeoutError("QueuePool timeout password=do-not-log"),
            "pool_timeout",
            None,
        ),
    ],
)
def test_tenant_session_failure_emits_safe_database_signal(
    caplog,
    monkeypatch,
    error: Exception,
    failure_kind: str,
    sqlstate: str | None,
) -> None:
    """RLS and pool failures must be distinguishable without exception payloads."""

    session = _FakeSyncSession()
    monkeypatch.setattr(
        SyncSqlAlchemyProvider,
        "_get_tenant_engine",
        lambda self, organisation_id: object(),
    )
    monkeypatch.setattr(
        provider_sync,
        "sessionmaker",
        lambda **_kwargs: lambda: session,
    )
    organisation_id = uuid4()

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(type(error)),
        SyncSqlAlchemyProvider().tenant_session(organisation_id),
    ):
        raise error

    records = _event_records(caplog, "shared_tenancy_database")
    assert len(records) == 1
    record = records[0]
    assert record.operation == "tenant_session"
    assert record.outcome == "failed"
    assert record.organisation_id == str(organisation_id)
    assert record.failure_kind == failure_kind
    assert record.sqlstate == sqlstate
    assert record.duration_ms >= 0
    assert "do-not-log" not in caplog.text
    assert session.rolled_back
    assert session.closed


@pytest.mark.asyncio
async def test_async_tenant_session_uses_the_same_database_signal(
    caplog,
    monkeypatch,
) -> None:
    """Async API and worker sessions must expose the same RLS alert contract."""

    session = _FakeAsyncSession()
    monkeypatch.setattr(
        AsyncSqlAlchemyProvider,
        "_get_tenant_engine",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(
        provider_async,
        "async_sessionmaker",
        lambda *_args, **_kwargs: lambda: session,
    )
    organisation_id = uuid4()

    with caplog.at_level(logging.ERROR), pytest.raises(_RlsViolationError):
        async with AsyncSqlAlchemyProvider().tenant_session(organisation_id):
            raise _RlsViolationError("password=do-not-log")

    records = _event_records(caplog, "shared_tenancy_database")
    assert len(records) == 1
    record = records[0]
    assert record.organisation_id == str(organisation_id)
    assert record.failure_kind == "rls_violation"
    assert record.sqlstate == "42501"
    assert "do-not-log" not in caplog.text
    assert session.rolled_back
    assert session.closed


def test_copy_validation_failure_emits_safe_cutover_signal(
    caplog,
    monkeypatch,
    tmp_path,
) -> None:
    """A failed cutover report must also reach the central alert stream."""

    organisation_id = uuid4()
    report_path = tmp_path / "cutover.json"

    def fail_copy(_args):
        """Inject one stable cutover error without database access."""

        raise cutover_shared_tenant_schema.CutoverError(
            "checksum_mismatch",
            "row payload=do-not-log",
        )

    monkeypatch.setattr(cutover_shared_tenant_schema, "run", fail_copy)
    with caplog.at_level(logging.ERROR):
        exit_code = cutover_shared_tenant_schema.main(
            [
                "copy-and-validate",
                "--organisation-id",
                str(organisation_id),
                "--report",
                str(report_path),
                "--writers-stopped-evidence",
                "queue-drain-report",
                "--backup-reference",
                "restore-proof",
                "--apply",
            ]
        )

    assert exit_code == 1
    records = _event_records(caplog, "shared_tenancy_cutover")
    assert len(records) == 1
    record = records[0]
    assert record.operation == "copy-and-validate"
    assert record.outcome == "failed"
    assert record.report_id
    assert record.organisation_id == str(organisation_id)
    assert record.error_code == "checksum_mismatch"
    assert record.duration_ms >= 0
    assert "do-not-log" not in caplog.text


def test_tenant_lifecycle_failure_emits_safe_operation_signal(
    caplog,
    monkeypatch,
) -> None:
    """Disaster-recovery commands must be alertable without row payloads."""

    organisation_id = uuid4()

    def fail_export(_args):
        """Inject one stable lifecycle refusal without database access."""

        raise cutover_shared_tenant_schema.CutoverError(
            "manifest_write_failed",
            "row payload=do-not-log",
        )

    monkeypatch.setattr(manage_shared_tenant_data, "run", fail_export)
    with caplog.at_level(logging.ERROR):
        exit_code = manage_shared_tenant_data.main(
            [
                "export",
                "--organisation-id",
                str(organisation_id),
                "--destination",
                "ignored",
            ]
        )

    assert exit_code == 1
    records = _event_records(caplog, "shared_tenancy_lifecycle")
    assert len(records) == 1
    record = records[0]
    assert record.operation == "export"
    assert record.outcome == "failed"
    assert record.organisation_id == str(organisation_id)
    assert record.error_code == "manifest_write_failed"
    assert record.duration_ms >= 0
    assert "do-not-log" not in caplog.text
