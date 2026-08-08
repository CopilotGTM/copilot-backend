from pathlib import Path

LOCAL_TERRAFORM_ROOT = Path(__file__).parents[2] / "infra" / "terraform" / "azure" / "local"


def test_local_terraform_uses_only_the_master_key_vault() -> None:
    main = (LOCAL_TERRAFORM_ROOT / "main.tf").read_text(encoding="utf-8")
    outputs = (LOCAL_TERRAFORM_ROOT / "outputs.tf").read_text(encoding="utf-8")
    readme = (LOCAL_TERRAFORM_ROOT / "README.md").read_text(encoding="utf-8")

    assert "database_key_vault" not in main
    assert "azurerm_key_vault.master_key_vault.id" in main
    assert 'output "key_vault_uri"' not in outputs
    assert "master_key_vault_uri" in outputs
    assert "`key_vault_uri`" not in readme
