"""Contract tests for the shared-row model and catalog readiness gate."""

from __future__ import annotations

from scripts.maintenance.audit_shared_tenant_readiness import (
    _catalog_security_report,
    _tenant_function_report,
    audit_metadata,
)
from sqlalchemy import (
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)


def _tenant_table(
    metadata: MetaData,
    name: str,
    *columns: Column,
    constraints: tuple[object, ...] = (),
) -> Table:
    return Table(
        name,
        metadata,
        Column("id", Integer, primary_key=True),
        *columns,
        *constraints,
    )


def _audit(
    metadata: MetaData,
    *,
    tenant_tables: set[str] | None = None,
    global_tables: set[str] | None = None,
    global_unique_rules: set[tuple[str, tuple[str, ...]]] | None = None,
) -> dict[str, object]:
    kwargs = {
        "tenant_tables": tenant_tables or set(),
        "global_tables": global_tables or set(),
    }
    if global_unique_rules is not None:
        kwargs["global_unique_rules"] = global_unique_rules
    return audit_metadata(metadata, **kwargs)


def test_report_contract_and_unclassified_table() -> None:
    """Unclassified tables must fail through the stable deployment report."""

    metadata = MetaData()
    Table("mystery", metadata, Column("id", Integer, primary_key=True))

    report = _audit(metadata)

    assert report == {
        "table_count": 1,
        "unclassified": ["mystery"],
        "missing_organisation_id": [],
        "nullable_organisation_id": [],
        "unscoped_unique_rules": [],
        "unscoped_tenant_foreign_keys": [],
        "missing_tenant_indexes": [],
        "missing_deterministic_keys": [],
        "rls_not_enabled": [],
        "rls_not_forced": [],
        "missing_policies": [],
        "unsafe_runtime_roles": [],
        "ok": False,
    }


def test_detects_tenant_table_without_organisation_id() -> None:
    """A missing tenant key must not hide the table's other unsafe rules."""

    metadata = MetaData()
    _tenant_table(
        metadata,
        "widgets",
        Column("external_id", String, nullable=False),
        constraints=(UniqueConstraint("external_id", name="uq_widgets_external_id"),),
    )

    report = _audit(metadata, tenant_tables={"widgets"})

    assert report["missing_organisation_id"] == ["widgets"]
    assert report["unscoped_unique_rules"] == ["widgets.uq_widgets_external_id"]


def test_detects_nullable_organisation_id() -> None:
    """Nullable tenant identity cannot enforce shared-row ownership."""

    metadata = MetaData()
    table = _tenant_table(
        metadata,
        "widgets",
        Column("organisation_id", String, nullable=True),
    )
    Index("ix_widgets_organisation_id", table.c.organisation_id)

    report = _audit(metadata, tenant_tables={"widgets"})

    assert report["nullable_organisation_id"] == ["widgets"]


def test_detects_unscoped_unique_constraint() -> None:
    """Business uniqueness must include the tenant boundary."""

    metadata = MetaData()
    table = _tenant_table(
        metadata,
        "widgets",
        Column("organisation_id", String, nullable=False),
        Column("external_id", String, nullable=False),
        constraints=(UniqueConstraint("external_id", name="uq_widgets_external_id"),),
    )
    Index("ix_widgets_organisation_id", table.c.organisation_id)

    report = _audit(metadata, tenant_tables={"widgets"})

    assert report["unscoped_unique_rules"] == ["widgets.uq_widgets_external_id"]


def test_intentional_global_unique_rule_requires_exact_classification() -> None:
    """Shared-resource keys stay global only through a reviewed exact rule."""

    metadata = MetaData()
    table = _tenant_table(
        metadata,
        "attachments",
        Column("organisation_id", String, nullable=False),
        Column("storage_path", String, nullable=False),
        constraints=(UniqueConstraint("storage_path", name="uq_attachment_path"),),
    )
    Index("ix_attachments_organisation_id", table.c.organisation_id)

    unclassified = _audit(metadata, tenant_tables={"attachments"})
    classified = _audit(
        metadata,
        tenant_tables={"attachments"},
        global_unique_rules={("attachments", ("storage_path",))},
    )

    assert unclassified["unscoped_unique_rules"] == ["attachments.uq_attachment_path"]
    assert classified["unscoped_unique_rules"] == []


def test_detects_tenant_foreign_key_without_organisation_id() -> None:
    """Tenant child links must not be able to reference another tenant."""

    metadata = MetaData()
    parent = _tenant_table(
        metadata,
        "parents",
        Column("organisation_id", String, nullable=False),
    )
    child = _tenant_table(
        metadata,
        "children",
        Column("organisation_id", String, nullable=False),
        Column("parent_id", Integer, nullable=False),
        constraints=(
            ForeignKeyConstraint(
                ["parent_id"],
                ["parents.id"],
                name="fk_children_parent",
            ),
        ),
    )
    Index("ix_parents_organisation_id", parent.c.organisation_id)
    Index("ix_children_organisation_id", child.c.organisation_id)

    report = _audit(metadata, tenant_tables={"parents", "children"})

    assert report["unscoped_tenant_foreign_keys"] == ["children.fk_children_parent"]


def test_detects_tenant_foreign_key_without_remote_organisation_id() -> None:
    """Naming the local tenant key is insufficient when the parent omits it."""

    metadata = MetaData()
    parent = _tenant_table(
        metadata,
        "parents",
        Column("organisation_id", String, nullable=False),
    )
    child = _tenant_table(
        metadata,
        "children",
        Column("organisation_id", String, nullable=False),
        Column("parent_id", Integer, nullable=False),
        constraints=(
            ForeignKeyConstraint(
                ["organisation_id", "parent_id"],
                ["parents.id", "parents.id"],
                name="fk_children_parent_without_remote_org",
            ),
        ),
    )
    Index("ix_parents_organisation_id", parent.c.organisation_id)
    Index("ix_children_organisation_id", child.c.organisation_id)

    report = _audit(metadata, tenant_tables={"parents", "children"})

    assert report["unscoped_tenant_foreign_keys"] == [
        "children.fk_children_parent_without_remote_org"
    ]


def test_detects_tenant_foreign_key_with_crossed_organisation_mapping() -> None:
    """Tenant identity must map to tenant identity in the first FK position."""

    metadata = MetaData()
    parent = _tenant_table(
        metadata,
        "parents",
        Column("organisation_id", String, nullable=False),
    )
    child = _tenant_table(
        metadata,
        "children",
        Column("organisation_id", String, nullable=False),
        Column("parent_id", Integer, nullable=False),
        constraints=(
            ForeignKeyConstraint(
                ["organisation_id", "parent_id"],
                ["parents.id", "parents.organisation_id"],
                name="fk_children_parent_crossed_org",
            ),
        ),
    )
    Index("ix_parents_organisation_id", parent.c.organisation_id)
    Index("ix_children_organisation_id", child.c.organisation_id)

    report = _audit(metadata, tenant_tables={"parents", "children"})

    assert report["unscoped_tenant_foreign_keys"] == ["children.fk_children_parent_crossed_org"]


def test_detects_tenant_index_whose_tenant_key_is_not_leading() -> None:
    """An index cannot bound a tenant scan when another column leads it."""

    metadata = MetaData()
    table = _tenant_table(
        metadata,
        "widgets",
        Column("organisation_id", String, nullable=False),
        Column("updated_at", Integer, nullable=False),
    )
    Index("ix_widgets_updated_org", table.c.updated_at, table.c.organisation_id)

    report = _audit(metadata, tenant_tables={"widgets"})

    assert report["missing_tenant_indexes"] == ["widgets"]


def test_tenant_leading_unique_constraint_is_a_usable_index() -> None:
    """PostgreSQL backs unique constraints with indexes usable by tenant scans."""

    metadata = MetaData()
    _tenant_table(
        metadata,
        "widgets",
        Column("organisation_id", String, nullable=False),
        Column("external_id", String, nullable=False),
        constraints=(
            UniqueConstraint(
                "organisation_id",
                "external_id",
                name="uq_widgets_org_external",
            ),
        ),
    )

    report = _audit(metadata, tenant_tables={"widgets"})

    assert report["missing_tenant_indexes"] == []


def test_detects_table_without_deterministic_primary_or_candidate_key() -> None:
    """Migration copy and checksum ordering require a deterministic row key."""

    metadata = MetaData()
    table = Table(
        "events",
        metadata,
        Column("organisation_id", String, nullable=False),
        Column("payload", String),
    )
    Index("ix_events_organisation_id", table.c.organisation_id)

    report = _audit(metadata, tenant_tables={"events"})

    assert report["missing_deterministic_keys"] == ["events"]


def test_explicit_global_table_is_not_treated_as_tenant_data() -> None:
    """Reviewed global data is exempt from tenant-only invariants."""

    metadata = MetaData()
    Table("countries", metadata, Column("code", String, primary_key=True))

    report = _audit(metadata, global_tables={"countries"})

    assert report["unclassified"] == []
    assert report["missing_organisation_id"] == []
    assert report["missing_tenant_indexes"] == []
    assert report["ok"] is True


def test_catalog_requires_both_policy_kinds_with_read_and_write_checks() -> None:
    """One incomplete RLS policy must never satisfy the production gate."""

    report = _catalog_security_report(
        [
            {
                "table_name": "widgets",
                "rls_enabled": True,
                "rls_forced": True,
                "owner_name": "migration_owner",
                "has_permissive_policy": True,
                "has_restrictive_policy": False,
                "policies_have_read_and_write_checks": True,
                "runtime_has_table_privileges": True,
            }
        ],
        runtime_role="app_tenant_runtime",
    )

    assert report["missing_policies"] == ["widgets"]


def test_catalog_requires_the_exact_fail_closed_context_function() -> None:
    report = _tenant_function_report(
        {
            "returns_uuid": True,
            "is_stable": True,
            "is_security_invoker": True,
            "has_safe_search_path": True,
            "reads_tenant_setting": False,
            "runtime_can_execute": True,
            "runtime_has_schema_usage": True,
            "public_can_execute": False,
        },
        schema="tenant_data",
    )

    assert report["missing_policies"] == ["tenant_data.current_organisation_id"]
