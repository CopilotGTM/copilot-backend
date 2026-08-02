from pathlib import Path

MIGRATION = Path(
    "migrations/master_migrations/versions/f1a2b3c4d5ee_012_add_user_authentication_fields.py"
)
REMOVED_MIGRATION = Path(
    "migrations/master_migrations/versions/f1a2b3c4d5ef_013_add_user_authentication_method.py"
)


def test_authentication_method_migration_contract() -> None:
    """One unreleased revision must own both new user authentication fields."""
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "f1a2b3c4d5ee"' in source
    assert 'down_revision = "f1a2b3c4d5ed"' in source
    assert '"profile_image_url"' in source
    assert '"authentication_method"' in source
    assert 'server_default="password"' in source
    assert '"ck_users_authentication_method"' in source
    assert "'google_workspace'" in source
    assert "'microsoft_entra'" in source
    assert source.count("if_exists=True") == 3
    assert not REMOVED_MIGRATION.exists()
