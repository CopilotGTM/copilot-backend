from unittest.mock import Mock

import scripts.run_master_migrations as migrations


def test_store_password_uses_master_key_vault(monkeypatch) -> None:
    client = Mock()
    secret_client_factory = Mock(return_value=client)
    credential = object()

    monkeypatch.delenv("AZURE_KEY_VAULT_URL", raising=False)
    monkeypatch.setenv("AZURE_MASTER_KEY_VAULT_URL", "https://master.vault.azure.net/")
    monkeypatch.setenv("ENV", "local")
    monkeypatch.setattr(migrations, "DefaultAzureCredential", lambda: credential)
    monkeypatch.setattr(migrations, "SecretClient", secret_client_factory)

    migrations._store_password_in_key_vault("master-copilot-role-password", "secret")

    secret_client_factory.assert_called_once_with(
        vault_url="https://master.vault.azure.net/",
        credential=credential,
    )
    client.set_secret.assert_called_once_with("master-copilot-role-password", "secret")
