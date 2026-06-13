from __future__ import annotations

from pathlib import Path

from src.domain.models.organisation_tables.sync import crm
from src.domain.models.organisation_tables.sync.crm import stakeholders

ROOT = Path(__file__).resolve().parents[1]


def test_old_deal_stakeholder_model_remains_available_until_table_retirement() -> None:
    assert hasattr(stakeholders, "DealStakeholder")
    assert "DealStakeholder" in crm.__all__


def test_no_drop_deal_stakeholders_migration_exists_in_participant_cutover() -> None:
    migration_dir = ROOT / "migrations/org_migrations/versions"
    offenders = [
        path.name
        for path in migration_dir.glob("*.py")
        if 'op.drop_table("deal_stakeholders")' in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_privacy_deletion_does_not_update_stale_deal_stakeholders() -> None:
    source = Path(
        "src/service/integrations/webhooks/hubspot/contacts/privacy_deletion.py"
    ).read_text(encoding="utf-8")

    assert "DealStakeholder" not in source
    assert "deal_stakeholders" not in source
