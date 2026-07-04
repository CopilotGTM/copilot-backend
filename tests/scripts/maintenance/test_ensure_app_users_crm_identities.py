from __future__ import annotations

from pathlib import Path


def test_app_user_identity_repair_only_uses_internal_crm_integration() -> None:
    source = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "maintenance"
        / "ensure_app_users_crm_identities.py"
    ).read_text(encoding="utf-8")

    assert "AND integration_type = 'internal_crm'" in source
    assert "integration_type IN ('internal_crm', 'salesforce', 'hubspot')" not in source
    assert "'internal_crm'," in source
