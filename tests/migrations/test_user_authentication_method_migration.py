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


def test_authentication_migration_adds_new_user_event_enum_labels() -> None:
    """The auth audit events must exist in PostgreSQL before the routes use them."""
    source = MIGRATION.read_text(encoding="utf-8")

    assert "ALTER TYPE public.usereventtype" in source
    assert "ADD VALUE IF NOT EXISTS" in source
    for label in (
        "IDENTITY_LINKED",
        "AUTH_METHOD_REJECTED",
        "SESSION_REVOKED",
        "RECOVERY_SUPPRESSED",
    ):
        assert f'"{label}"' in source
