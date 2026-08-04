from pathlib import Path

MIGRATION = Path(
    "migrations/master_migrations/versions/f1a2b3c4d5ee_012_add_user_authentication_fields.py"
)


def test_external_signup_migration_contract() -> None:
    """Unreleased revision 012 owns auth and signup schema in one migration."""
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "f1a2b3c4d5ee"' in source
    assert 'down_revision = "f1a2b3c4d5ed"' in source
    assert '"authentication_method"' in source
    assert '"signup_requests"' in source
    assert '"primary_domain"' in source
    assert '"uq_organisations_primary_domain"' in source
    assert '"uq_signup_provider_subject_tenant"' in source
    assert '"organisations_organisation_name_key"' in source
    assert 'server_default="draft"' in source
    assert '"profile_incomplete"' in source
    assert "op.drop_constraint(" in source


def test_external_signup_does_not_create_a_second_unreleased_revision() -> None:
    """The unreleased signup schema remains atomic in revision 012."""
    assert not Path(
        "migrations/master_migrations/versions/f1a2b3c4d5ef_013_add_external_signup.py"
    ).exists()
