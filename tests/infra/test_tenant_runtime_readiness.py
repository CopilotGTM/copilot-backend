"""Contract tests for the API and worker shared-tenant startup gate."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from src.infra.database.tenant_runtime_readiness import (
    TenantRuntimeReadinessError,
    validate_tenant_runtime_catalog,
    validate_tenant_runtime_catalog_async,
)


class _ScalarResult:
    def __init__(self, values: tuple[str, ...]) -> None:
        self.values = values

    def scalars(self) -> tuple[str, ...]:
        """Expose the subset of SQLAlchemy result behavior used by the gate."""

        return self.values


class _SyncSession:
    def __init__(self, failures: tuple[str, ...] = ()) -> None:
        self.failures = failures
        self.statements: list[tuple[str, object]] = []

    def execute(self, statement, parameters=None):  # noqa: ANN001
        """Record the transaction preamble and parameterized catalog query."""

        self.statements.append((str(statement), parameters))
        return _ScalarResult(()) if parameters is None else _ScalarResult(self.failures)


def test_catalog_gate_checks_the_complete_runtime_boundary() -> None:
    """Startup must inspect every privilege and RLS escape hatch in one gate."""

    session = _SyncSession()

    validate_tenant_runtime_catalog(
        session,  # type: ignore[arg-type]
        runtime_role="app_tenant_runtime",
    )

    assert session.statements[0][0] == "SET TRANSACTION READ ONLY"
    assert session.statements[1][0] == "SET LOCAL search_path TO pg_catalog"
    sql, parameters = session.statements[2]
    assert parameters == {
        "schema": "tenant_data",
        "runtime_role": "app_tenant_runtime",
    }
    for required_contract in (
        "relrowsecurity",
        "relforcerowsecurity",
        "missing DML grants",
        "invalid tenant policies",
        "policy_record.polroles",
        "runtime can assume owner",
        "rolbypassrls",
        "missing USAGE or runtime has CREATE",
        "current_organisation_id",
        "public_can_execute",
    ):
        assert required_contract in sql


def test_catalog_gate_reports_every_unsafe_finding() -> None:
    """Operators need all findings so one restart can verify every repair."""

    session = _SyncSession(
        (
            "table crm_deals: RLS not forced",
            "runtime role: unsafe attributes or inherited elevation",
        )
    )

    with pytest.raises(
        TenantRuntimeReadinessError,
        match="RLS not forced.*unsafe attributes",
    ):
        validate_tenant_runtime_catalog(
            session,  # type: ignore[arg-type]
            runtime_role="app_tenant_runtime",
        )


@pytest.mark.asyncio
async def test_async_catalog_gate_has_identical_failure_behavior() -> None:
    """API and Celery startup must reject the same unsafe catalog."""

    session = AsyncMock()
    session.execute.side_effect = (
        _ScalarResult(()),
        _ScalarResult(()),
        _ScalarResult(("context function: missing or unsafe",)),
    )

    with pytest.raises(TenantRuntimeReadinessError, match="context function"):
        await validate_tenant_runtime_catalog_async(
            session,
            runtime_role="app_tenant_runtime",
        )

    assert session.execute.await_count == 3
    assert str(session.execute.await_args_list[1].args[0]) == (
        "SET LOCAL search_path TO pg_catalog"
    )
