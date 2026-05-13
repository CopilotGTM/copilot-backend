from __future__ import annotations

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


def test_org_migration_head_repairs_crm_deal_stage_is_active_for_stamped_schemas() -> None:
    head_source = _org_migration_head_source()

    assert "crm_deal_stages" in head_source
    assert "is_active" in head_source
    assert "if_not_exists=True" in head_source


def test_org_migration_head_adds_deal_scale_indexes() -> None:
    head_source = _org_migration_head_source()

    assert "ix_crm_deal_org_active_created" in head_source
    assert "ix_crm_deal_participant_org_deal_type_active" in head_source
    assert "ix_deal_stakeholder_org_deal_email_active" in head_source
