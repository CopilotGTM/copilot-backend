from __future__ import annotations

from pathlib import Path

from src.domain.models.organisation_tables.sync.crm import stakeholders

ROOT = Path(__file__).resolve().parents[1]


def test_old_deal_stakeholder_model_is_removed_after_table_retirement() -> None:
    assert not hasattr(stakeholders, "DealStakeholder")


def test_drop_deal_stakeholders_migration_exists_after_participant_cutover() -> None:
    migration_dir = ROOT / "migrations/org_migrations/versions"
    drop_migrations = [
        path.name
        for path in migration_dir.glob("*.py")
        if 'op.drop_table("deal_stakeholders")' in path.read_text(encoding="utf-8")
    ]

    assert drop_migrations == ["3c7f79637738_061_drop_deal_stakeholders.py"]


def test_org_migration_metadata_does_not_register_stale_deal_stakeholders() -> None:
    env_source = Path("migrations/org_migrations/env.py").read_text(encoding="utf-8")

    assert "DealStakeholder" not in env_source


def test_privacy_deletion_does_not_update_stale_deal_stakeholders() -> None:
    source = Path(
        "src/service/integrations/webhooks/hubspot/contacts/privacy_deletion.py"
    ).read_text(encoding="utf-8")

    assert "DealStakeholder" not in source
    assert "deal_stakeholders" not in source
