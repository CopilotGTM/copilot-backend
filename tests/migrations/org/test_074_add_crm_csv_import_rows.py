from pathlib import Path

MIGRATION_PATH = Path(
    "migrations/org_migrations/versions/"
    "3c7f79637725_074_add_crm_csv_import_rows.py"
)


def test_crm_csv_import_row_migration_creates_only_row_ledger() -> None:
    migration = MIGRATION_PATH.read_text(encoding="utf-8")

    assert 'revision: str = "3c7f79637725"' in migration
    assert 'down_revision: str | None = "3c7f79637726"' in migration
    assert '"crm_import_rows"' in migration
    assert '"crm_import_jobs"' not in migration
    assert '"deal_jobs.job_id"' in migration
    assert "ondelete=\"CASCADE\"" in migration
    assert "JSONB" in migration
    assert '"deal_id"' in migration
    assert "ux_crm_import_rows_import_row_number" in migration
    assert "ix_crm_import_rows_import_status" in migration
    assert "ix_crm_import_rows_org_status" in migration


def test_crm_csv_import_row_model_and_job_type_are_exported() -> None:
    from src.domain.models.organisation_tables.job_models import DealJobType
    from src.domain.models.organisation_tables.sync.crm import CRMImportRow

    assert DealJobType.CRM_CSV_IMPORT == "crm_csv_import"
    assert CRMImportRow.__tablename__ == "crm_import_rows"
    assert hasattr(CRMImportRow, "deal_id")
