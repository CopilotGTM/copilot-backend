"""Contracts for the CRM tenant-integrity migration."""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
from types import ModuleType
from typing import Any

from scripts.maintenance.audit_shared_tenant_readiness import (
    _all_sqlmodel_subclasses,
    _organisation_tables,
    _referred_table_names,
    build_model_report,
)
from sqlalchemy import Index
from sqlalchemy.sql.schema import (
    ForeignKeyConstraint,
    PrimaryKeyConstraint,
    UniqueConstraint,
)

CRM_MODULE_PREFIX = "src.domain.models.organisation_tables.sync.crm"
MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "migrations"
    / "org_migrations"
    / "versions"
    / "3c7f79637716_083_harden_crm_tenant_integrity.py"
)
REVISION_087_CRM_TENANT_FOREIGN_KEYS = {
    (
        "crm_mutation_events",
        "caused_by_run_id",
        "agent_runs",
        "run_id",
        "RESTRICT",
    ),
}


def _load_migration() -> ModuleType:
    assert MIGRATION_PATH.exists(), f"missing migration: {MIGRATION_PATH.name}"
    spec = importlib.util.spec_from_file_location("org_migration_083", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _crm_table_names() -> set[str]:
    _organisation_tables()
    return {
        model.__table__.name
        for model in _all_sqlmodel_subclasses()
        if model.__module__.startswith(CRM_MODULE_PREFIX)
        and hasattr(model, "__table__")
    }


def _crm_foreign_key_specs() -> set[tuple[str, str, str, str, str | None]]:
    tables = _organisation_tables()
    tenant_names = {table.name for table in tables}
    crm_names = _crm_table_names()
    specs: set[tuple[str, str, str, str, str | None]] = set()

    for table in tables:
        if table.name not in crm_names:
            continue
        for constraint in table.constraints:
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            if not (_referred_table_names(constraint) & tenant_names):
                continue
            elements = tuple(constraint.elements)
            assert len(elements) == 2, f"{table.name}.{constraint.name}"
            assert elements[0].parent.name == "organisation_id"
            assert (
                elements[0].target_fullname.rsplit(".", 1)[-1]
                == "organisation_id"
            )
            if (
                constraint.name
                != f"fk_{table.name}_{elements[1].parent.name}_org"
            ):
                continue
            specs.add(
                (
                    table.name,
                    elements[1].parent.name,
                    elements[1].target_fullname.rsplit(".", 1)[0].rsplit(".", 1)[-1],
                    elements[1].target_fullname.rsplit(".", 1)[-1],
                    constraint.ondelete,
                )
            )
    return specs


def _has_unique_key(table: Any, columns: tuple[str, ...]) -> bool:
    for constraint in table.constraints:
        if isinstance(
            constraint,
            (PrimaryKeyConstraint, UniqueConstraint),
        ) and tuple(column.name for column in constraint.columns) == columns:
            return True
    return any(
        index.unique and tuple(column.name for column in index.columns) == columns
        for index in table.indexes
    )


def _has_leading_index(table: Any, columns: tuple[str, ...]) -> bool:
    indexes: list[Index | UniqueConstraint | PrimaryKeyConstraint] = list(
        table.indexes
    )
    indexes.extend(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, (PrimaryKeyConstraint, UniqueConstraint))
    )
    return any(
        tuple(column.name for column in index.columns)[: len(columns)] == columns
        for index in indexes
    )


def test_crm_models_have_only_tenant_qualified_integrity_rules() -> None:
    """Task 4 must clear only the CRM-owned structural findings."""

    crm_names = _crm_table_names()
    report = build_model_report()

    for field in ("unscoped_tenant_foreign_keys", "unscoped_unique_rules"):
        crm_findings = [
            finding
            for finding in report[field]
            if finding.split(".", 1)[0] in crm_names
        ]
        assert crm_findings == [], field


def test_crm_foreign_keys_have_candidate_keys_and_child_indexes() -> None:
    """Every composite relationship needs both PostgreSQL supporting indexes."""

    tables = {table.name: table for table in _organisation_tables()}

    for child, local_id, parent, remote_id, _ondelete in _crm_foreign_key_specs():
        assert _has_unique_key(
            tables[parent],
            ("organisation_id", remote_id),
        ), f"{parent}.organisation_id_{remote_id}"
        assert _has_leading_index(
            tables[child],
            ("organisation_id", local_id),
        ), f"{child}.organisation_id_{local_id}"


def test_revision_083_matches_the_model_derived_relationship_inventory() -> None:
    """The immutable migration inventory must cover every current CRM relation."""

    migration = _load_migration()
    tables = {table.name: table for table in _organisation_tables()}
    current_relationships = _crm_foreign_key_specs()
    assert current_relationships >= REVISION_087_CRM_TENANT_FOREIGN_KEYS
    relationships = current_relationships - REVISION_087_CRM_TENANT_FOREIGN_KEYS

    assert migration.revision == "3c7f79637716"
    assert migration.down_revision == "3c7f79637717"
    assert set(migration.CRM_TENANT_FOREIGN_KEYS) == relationships
    assert {
        (parent, remote_id)
        for _child, _local_id, parent, remote_id, _ondelete in (
            migration.CRM_TENANT_FOREIGN_KEYS
        )
    } == set(migration.CRM_CANDIDATE_KEYS)
    assert set(migration.CRM_FOREIGN_KEY_INDEXES) == {
        (child, local_id)
        for child, local_id, _parent, _remote_id, _ondelete in relationships
        if f"ix_{child}_org_{local_id}"
        in {index.name for index in tables[child].indexes}
    }
    for (
        table_name,
        legacy_name,
        scoped_name,
        columns,
        predicate,
    ) in migration.CRM_SCOPED_UNIQUE_INDEXES:
        indexes = {index.name: index for index in tables[table_name].indexes}
        assert legacy_name not in indexes
        scoped_index = indexes[scoped_name]
        assert scoped_index.unique is True
        assert tuple(column.name for column in scoped_index.columns) == columns
        model_predicate = scoped_index.dialect_options["postgresql"].get("where")
        assert (
            str(model_predicate) if model_predicate is not None else None
        ) == predicate


def test_revision_083_validates_new_constraints_before_removing_legacy_rules() -> None:
    """The old protection remains until every replacement has been validated."""

    migration = _load_migration()
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    upgrade_source = inspect.getsource(migration.upgrade)

    create_position = upgrade_source.index("op.create_foreign_key")
    validate_position = upgrade_source.index("_validate_constraint")
    drop_position = upgrade_source.index(
        "_drop_legacy_foreign_key",
        validate_position,
    )

    assert "postgresql_not_valid=True" in upgrade_source
    assert "postgresql_concurrently=True" in source
    assert create_position < validate_position < drop_position
    assert "CRM_SCOPED_UNIQUE_INDEXES" in source
    assert "CRM_FOREIGN_KEY_INDEXES" in source
