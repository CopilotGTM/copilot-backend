from __future__ import annotations

from azure.keyvault.secrets import SecretClient
from src.infra.config.configs import secret_manager_config
from src.infra.secrets.azure_credentials import build_sync_azure_credential


def build_master_secret_client_sync(*, prefer_default_credential: bool = False) -> SecretClient:
    if not secret_manager_config.AZURE_MASTER_KEY_VAULT_URL:
        raise RuntimeError("AZURE_MASTER_KEY_VAULT_URL is required")
    return SecretClient(
        vault_url=secret_manager_config.AZURE_MASTER_KEY_VAULT_URL,
        credential=build_sync_azure_credential(prefer_default_credential=prefer_default_credential),
    )


def get_master_key_vault_secret_sync(name: str) -> str | None:
    secret = build_master_secret_client_sync().get_secret(name)
    return getattr(secret, "value", None)
