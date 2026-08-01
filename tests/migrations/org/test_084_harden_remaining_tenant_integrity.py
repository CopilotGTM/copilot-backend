"""Contracts for the remaining tenant-integrity migration."""

from __future__ import annotations

import importlib.util
import inspect
import warnings
from pathlib import Path
from types import ModuleType
from typing import Any

import scripts.maintenance.audit_shared_tenant_readiness as audit_module
from scripts.maintenance.audit_shared_tenant_readiness import (
    _all_sqlmodel_subclasses,
    _organisation_tables,
    _referred_table_names,
    build_model_report,
)
from sqlalchemy import Index
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SAWarning
from sqlalchemy.orm import configure_mappers
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql.schema import (
    ForeignKeyConstraint,
    PrimaryKeyConstraint,
    UniqueConstraint,
)
from src.domain.models.master_tables.organisation_models import Organisation

CRM_MODULE_PREFIX = "src.domain.models.organisation_tables.sync.crm"
MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "migrations"
    / "org_migrations"
    / "versions"
    / "3c7f79637715_084_harden_remaining_tenant_integrity.py"
)
GLOBAL_STORAGE_RULES = {
    ("attachments", ("storage_path",)),
    ("deal_assets", ("storage_path",)),
    ("organisation_sources", ("storage_path",)),
}
RETIRED_TABLES = {
    "ci_call_analytics",
    "ci_call_deal_links",
    "ci_call_speakers",
    "ci_calls",
    "ci_transcript_segments",
    "deal_timeline_events",
    "email_thread_summaries",
    "integration_participant_identities",
    "messaging_thread_summaries",
}


def _load_migration() -> ModuleType:
    assert MIGRATION_PATH.exists(), f"missing migration: {MIGRATION_PATH.name}"
    spec = importlib.util.spec_from_file_location("org_migration_084", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _models_by_table() -> dict[str, type[Any]]:
    _organisation_tables()
    return {
        model.__table__.name: model
        for model in _all_sqlmodel_subclasses()
        if hasattr(model, "__table__")
    }


def _remaining_foreign_key_specs() -> set[tuple[str, str, str, str, str | None]]:
    tables = _organisation_tables()
    tenant_names = {table.name for table in tables}
    models = _models_by_table()
    specs: set[tuple[str, str, str, str, str | None]] = set()

    for table in tables:
        model = models[table.name]
        if model.__module__.startswith(CRM_MODULE_PREFIX):
            continue
        for constraint in table.constraints:
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            if not (_referred_table_names(constraint) & tenant_names):
                continue
            elements = tuple(constraint.elements)
            if len(elements) != 2:
                continue
            local_id = elements[1].parent.name
            if constraint.name != f"fk_{table.name}_{local_id}_org":
                continue
            assert elements[0].parent.name == "organisation_id"
            assert (
                elements[0].target_fullname.rsplit(".", 1)[-1]
                == "organisation_id"
            )
            specs.add(
                (
                    table.name,
                    local_id,
                    elements[1].target_fullname.rsplit(".", 1)[0].rsplit(".", 1)[
                        -1
                    ],
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
    candidates: list[Index | UniqueConstraint | PrimaryKeyConstraint] = list(
        table.indexes
    )
    candidates.extend(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, (PrimaryKeyConstraint, UniqueConstraint))
    )
    return any(
        tuple(column.name for column in candidate.columns)[: len(columns)] == columns
        for candidate in candidates
    )


def test_remaining_models_clear_the_structural_audit() -> None:
    """Task 5 must leave no unclassified relationship or uniqueness finding."""

    report = build_model_report()

    assert audit_module.INTENTIONAL_GLOBAL_UNIQUE_RULES == GLOBAL_STORAGE_RULES
    assert report["unscoped_tenant_foreign_keys"] == []
    assert report["unscoped_unique_rules"] == []


def test_remaining_models_configure_and_compile_without_mapper_warnings() -> None:
    """Composite tenant relationships must remain valid SQLAlchemy mappings."""

    tables = _organisation_tables()

    with warnings.catch_warnings():
        warnings.simplefilter("error", SAWarning)
        configure_mappers()
        for table in tables:
            str(CreateTable(table).compile(dialect=postgresql.dialect()))

    assert Organisation.__tablename__ == "organisations"


def test_revision_084_matches_model_derived_relationships() -> None:
    """One immutable migration inventory must cover every remaining relation."""

    migration = _load_migration()
    tables = {table.name: table for table in _organisation_tables()}
    relationships = _remaining_foreign_key_specs()
    active_migration_relationships = {
        relationship
        for relationship in migration.REMAINING_TENANT_FOREIGN_KEYS
        if relationship[0] not in RETIRED_TABLES
        and relationship[2] not in RETIRED_TABLES
    }
    active_candidate_keys = tuple(
        candidate
        for candidate in migration.REMAINING_CANDIDATE_KEYS
        if candidate[0] not in RETIRED_TABLES
    )
    active_foreign_key_indexes = tuple(
        index
        for index in migration.REMAINING_FOREIGN_KEY_INDEXES
        if index[0] not in RETIRED_TABLES
    )

    assert migration.revision == "3c7f79637715"
    assert migration.down_revision == "3c7f79637716"
    assert len(relationships) == 51
    assert active_migration_relationships == relationships
    assert len(active_candidate_keys) == 14
    assert len(active_foreign_key_indexes) == 3

    for child, local_id, parent, remote_id, _ondelete in relationships:
        assert _has_unique_key(
            tables[parent],
            ("organisation_id", remote_id),
        ), f"{parent}.organisation_id_{remote_id}"
        assert _has_leading_index(
            tables[child],
            (local_id,),
        ) or _has_leading_index(
            tables[child],
            ("organisation_id", local_id),
        ), f"{child}.{local_id}"

    for table_name, identity_column in active_candidate_keys:
        name = migration._candidate_name(table_name, identity_column)
        constraints = {constraint.name for constraint in tables[table_name].constraints}
        assert name in constraints

    for table_name, local_column in active_foreign_key_indexes:
        name = migration._foreign_key_index_name(table_name, local_column)
        assert name in {index.name for index in tables[table_name].indexes}


def test_revision_084_creates_missing_conversation_intelligence_tables() -> None:
    """A fresh org migration chain must contain every classified model table."""

    migration = _load_migration()
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert migration.CONVERSATION_INTELLIGENCE_TABLES == (
        "ci_calls",
        "ci_call_deal_links",
        "ci_call_speakers",
        "ci_transcript_segments",
        "ci_call_analytics",
    )
    for table_name in migration.CONVERSATION_INTELLIGENCE_TABLES:
        assert f'op.create_table(\n            "{table_name}"' in source

    upgrade_source = inspect.getsource(migration.upgrade)
    assert upgrade_source.index("_create_conversation_intelligence_tables") < (
        upgrade_source.index("for table_name, identity_column")
    )


def test_revision_084_matches_scoped_and_global_unique_rules() -> None:
    """Business identifiers become tenant-local while blob keys stay global."""

    migration = _load_migration()
    tables = {table.name: table for table in _organisation_tables()}
    active_migration_unique_rules = tuple(
        rule
        for rule in migration.REMAINING_SCOPED_UNIQUE_RULES
        if rule[0] not in RETIRED_TABLES
    )

    assert len(active_migration_unique_rules) == 18
    for (
        table_name,
        kind,
        _legacy_name,
        scoped_name,
        columns,
        predicate,
        deferrable,
        initially,
    ) in active_migration_unique_rules:
        table = tables[table_name]
        if kind == "constraint":
            matches = [
                constraint
                for constraint in table.constraints
                if isinstance(constraint, UniqueConstraint)
                and constraint.name == scoped_name
            ]
        else:
            matches = [index for index in table.indexes if index.name == scoped_name]
        assert len(matches) == 1
        rule = matches[0]
        assert tuple(column.name for column in rule.columns) == columns
        if isinstance(rule, Index):
            model_predicate = rule.dialect_options["postgresql"].get("where")
            assert (
                str(model_predicate) if model_predicate is not None else None
            ) == predicate
            assert deferrable is False
            assert initially is None
        else:
            assert predicate is None
            assert bool(rule.deferrable) is deferrable
            assert rule.initially == initially

    for table_name, columns in GLOBAL_STORAGE_RULES:
        table = tables[table_name]
        assert any(
            isinstance(rule, UniqueConstraint)
            and tuple(column.name for column in rule.columns) == columns
            for rule in table.constraints
        )


def test_revision_084_installs_replacements_before_removing_legacy_rules() -> None:
    """The database must retain protection throughout each online rule swap."""

    migration = _load_migration()
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    upgrade_source = inspect.getsource(migration.upgrade)

    create_fk = upgrade_source.index("op.create_foreign_key")
    validate_fk = upgrade_source.index("_validate_constraint")
    drop_fk = upgrade_source.index("_drop_legacy_foreign_key", validate_fk)
    create_unique = upgrade_source.index("_ensure_concurrent_index")
    drop_unique = upgrade_source.index("_drop_legacy_unique_rule", create_unique)

    assert "postgresql_not_valid=True" in upgrade_source
    assert "postgresql_concurrently=True" in source
    assert create_fk < validate_fk < drop_fk
    assert create_unique < drop_unique
