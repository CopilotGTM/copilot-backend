"""Contracts for Terraform ownership of the permanent tenant schema."""

from pathlib import Path

ENVIRONMENT_ROOT = Path("infra/terraform/azure")


def test_dev_and_prod_manage_one_protected_tenant_data_schema() -> None:
    """Both deployed environments must manage the same protected schema."""

    for environment in ("dev", "prod"):
        provider = (ENVIRONMENT_ROOT / environment / "provider.tf").read_text(encoding="utf-8")
        main = (ENVIRONMENT_ROOT / environment / "main.tf").read_text(encoding="utf-8")
        lock = (ENVIRONMENT_ROOT / environment / ".terraform.lock.hcl").read_text(encoding="utf-8")

        assert 'source  = "cyrilgdn/postgresql"' in provider
        assert 'version = "1.26.0"' in provider
        assert 'provider "postgresql"' in main
        assert "host            = module.application_database.db_host" in main
        assert 'sslmode         = "require"' in main
        assert main.count('resource "postgresql_schema" "tenant_data"') == 1
        assert 'name          = "tenant_data"' in main
        assert "owner         = module.application_database.db_user" in main
        assert "if_not_exists = true" in main
        assert "drop_cascade  = false" in main
        assert "prevent_destroy = true" in main
        assert 'provider "registry.terraform.io/cyrilgdn/postgresql"' in lock
        assert 'version     = "1.26.0"' in lock
