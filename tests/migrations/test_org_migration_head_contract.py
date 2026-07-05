"""Regression checks for organisation migration head/source contracts."""

from __future__ import annotations

import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def _org_migration_head_source() -> str:
    migrations_dir = Path(__file__).resolve().parents[2] / "migrations" / "org_migrations"
    config = Config(str(migrations_dir / "alembic.ini"))
    config.set_main_option("script_location", str(migrations_dir))

    script_dir = ScriptDirectory.from_config(config)
    head = script_dir.get_revision(script_dir.get_current_head())
    assert head is not None

    return Path(head.path).read_text(encoding="utf-8")


def _org_migration_sources() -> str:
    migrations_dir = Path(__file__).resolve().parents[2] / "migrations" / "org_migrations"
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((migrations_dir / "versions").glob("*.py"))
    )


def _org_migration_paths() -> list[Path]:
    migrations_dir = Path(__file__).resolve().parents[2] / "migrations" / "org_migrations"
    return sorted((migrations_dir / "versions").glob("*.py"))


def _crm_mutation_events_migration_source() -> str:
    migrations_dir = Path(__file__).resolve().parents[2] / "migrations" / "org_migrations"
    migration_path = (
        migrations_dir / "versions" / "3c7f79637737_062_add_crm_mutation_events.py"
    )
    return migration_path.read_text(encoding="utf-8")


def _crm_stage_key_migration_source() -> str:
    migrations_dir = Path(__file__).resolve().parents[2] / "migrations" / "org_migrations"
    migration_path = (
        migrations_dir
        / "versions"
        / "3c7f79637727_072_add_internal_stage_key_uniqueness.py"
    )
    return migration_path.read_text(encoding="utf-8")


def test_org_migration_head_repairs_crm_deal_stage_is_active_for_stamped_schemas() -> None:
    """Lock the additive stage repair pattern needed by already-stamped schemas."""
    head_source = _org_migration_sources()

    assert "crm_deal_stages" in head_source
    assert "is_active" in head_source
    assert "if_not_exists=True" in head_source


def test_org_migration_head_adds_deal_scale_indexes() -> None:
    """Lock the tenant/deal scale indexes used by CRM list and stakeholder reads."""
    head_source = _org_migration_sources()

    assert "ix_crm_deal_org_active_created" in head_source
    assert "ix_crm_deal_participant_org_deal_type_active" in head_source
    assert "ix_deal_stakeholder_org_deal_email_active" in head_source


def test_org_migration_head_adds_crm_mutation_events_outbox() -> None:
    """Lock the required CRM mutation event table and workflow-matchable fields."""
    head_source = _crm_mutation_events_migration_source()

    assert "crm_mutation_events" in head_source
    assert "ux_crm_mutation_events_idempotency" in head_source
    assert "event_type" in head_source
    assert "semantic_event_types" in head_source
    assert "ix_crm_mutation_events_semantic_event_types" in head_source
    assert "mutation_group_id" in head_source
    assert "deal_id" in head_source
    assert "occurred_at" in head_source
    assert "payload_hash" in head_source


def test_org_migration_head_adds_internal_stage_key_uniqueness() -> None:
    """Concurrent internal stage creates must not bypass service duplicate checks."""
    head_source = _crm_stage_key_migration_source()

    assert "ux_crm_deal_stages_internal_active_stage_key" in head_source
    assert "crm_deal_stages" in head_source
    assert "organisation_id" in head_source
    assert "pipeline_id" in head_source
    assert "stage_key" in head_source
    assert "is_deleted = false" in head_source
    assert "is_active = true" in head_source


def test_org_migration_revisions_and_sequence_numbers_are_unique() -> None:
    """Alembic revision IDs and numbered filenames must not collide after rebases."""
    revisions: dict[str, Path] = {}
    sequence_numbers: dict[str, Path] = {}

    for path in _org_migration_paths():
        source = path.read_text(encoding="utf-8")
        revision_match = re.search(
            r"""^revision(?:\s*:\s*str)?\s*=\s*["']([^"']+)["']""",
            source,
            re.M,
        )
        assert revision_match is not None, f"{path.name} is missing a revision"

        revision = revision_match.group(1)
        assert revision not in revisions, (
            f"{path.name} duplicates revision {revision} from {revisions[revision].name}"
        )
        revisions[revision] = path

        sequence_match = re.search(r"_(\d{3})_", path.name)
        assert sequence_match is not None, f"{path.name} is missing a migration sequence"

        sequence = sequence_match.group(1)
        assert sequence not in sequence_numbers, (
            f"{path.name} duplicates sequence {sequence} from "
            f"{sequence_numbers[sequence].name}"
        )
        sequence_numbers[sequence] = path


def test_crm_mutation_events_do_not_store_direct_workflow_lineage_columns() -> None:
    """Prevent ambiguous workflow lineage columns from returning to the CRM event table."""
    head_source = _crm_mutation_events_migration_source()

    # CRM mutation events are audit/outbox truth. Workflow fanout and workflow-caused
    # CRM writes need explicit runtime tables/lineage columns later, not ambiguous IDs here.
    assert "workflow_run_id" not in head_source
    assert "triggered_by_event_id" not in head_source


def test_crm_mutation_events_do_not_store_workflow_dispatch_runtime_columns() -> None:
    """Keep per-workflow claim/retry state out of the parent CRM mutation event."""
    head_source = _crm_mutation_events_migration_source()

    # One CRM mutation can match several workflow triggers, so claim/retry/error state
    # belongs in a future child dispatch table instead of the immutable CRM event row.
    assert "dispatch_status" not in head_source
    assert "attempt_count" not in head_source
    assert "claimed_at" not in head_source
    assert "claimed_by" not in head_source
    assert "last_error" not in head_source
    assert "dispatched_at" not in head_source
