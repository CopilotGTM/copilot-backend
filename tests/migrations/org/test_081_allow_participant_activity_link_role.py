"""Contract checks for the hydration participant-link role migration."""

from pathlib import Path


def test_081_allows_participant_activity_links_and_keeps_downgrade_safe() -> None:
    migration = (
        Path(__file__).resolve().parents[3]
        / "migrations"
        / "org_migrations"
        / "versions"
        / "3c7f79637718_081_allow_participant_activity_link_role.py"
    ).read_text(encoding="utf-8")

    assert 'revision: str = "3c7f79637718"' in migration
    assert 'down_revision: str | None = "3c7f79637719"' in migration
    assert "ck_crm_activity_link_role" in migration
    assert "'PARTICIPANT'" in migration
    assert "DELETE FROM crm_activity_links AS participant_link" in migration
    assert "SET link_role = 'RELATED'" in migration
