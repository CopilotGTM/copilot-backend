from __future__ import annotations

from src.infra.secrets import azure_credentials, master_key_vault_sync


def test_build_sync_azure_credential_uses_workload_identity(monkeypatch) -> None:
    created: list[tuple[str, str, list[str]]] = []

    class FakeWorkloadIdentityCredential:
        def __init__(
            self,
            *,
            tenant_id: str,
            client_id: str,
            additionally_allowed_tenants: list[str],
        ) -> None:
            created.append((tenant_id, client_id, additionally_allowed_tenants))

    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setattr(
        azure_credentials.cloud_infra_config,
        "AZURE_TENANT_ID",
        "tenant-id",
        raising=False,
    )
    monkeypatch.setattr(
        azure_credentials.cloud_infra_config,
        "AZURE_CLIENT_ID",
        "client-id",
        raising=False,
    )
    monkeypatch.setattr(
        azure_credentials.cloud_infra_config,
        "AZURE_ADDITIONALLY_ALLOWED_TENANTS",
        "extra-tenant",
        raising=False,
    )
    monkeypatch.setattr(
        azure_credentials,
        "WorkloadIdentityCredential",
        FakeWorkloadIdentityCredential,
    )

    credential = azure_credentials.build_sync_azure_credential()

    assert isinstance(credential, FakeWorkloadIdentityCredential)
    assert created == [("tenant-id", "client-id", ["extra-tenant"])]


def test_build_sync_azure_credential_can_force_default_credential(monkeypatch) -> None:
    created: list[str] = []

    class FakeDefaultAzureCredential:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            created.append(str(kwargs))

    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setattr(
        azure_credentials.cloud_infra_config,
        "AZURE_TENANT_ID",
        "tenant-id",
        raising=False,
    )
    monkeypatch.setattr(
        azure_credentials.cloud_infra_config,
        "AZURE_CLIENT_ID",
        "client-id",
        raising=False,
    )
    monkeypatch.setattr(
        azure_credentials,
        "DefaultAzureCredential",
        FakeDefaultAzureCredential,
    )

    credential = azure_credentials.build_sync_azure_credential(prefer_default_credential=True)

    assert isinstance(credential, FakeDefaultAzureCredential)
    assert created == [
        "{'exclude_interactive_browser_credential': True, "
        "'exclude_visual_studio_code_credential': False}"
    ]


def test_build_master_secret_client_sync_uses_shared_credential_helper(monkeypatch) -> None:
    credential = object()
    created: list[tuple[str, object]] = []

    class FakeSecretClient:
        def __init__(self, *, vault_url: str, credential: object) -> None:
            created.append((vault_url, credential))

    monkeypatch.setattr(
        master_key_vault_sync.secret_manager_config,
        "AZURE_MASTER_KEY_VAULT_URL",
        "https://master-vault.example",
        raising=False,
    )
    monkeypatch.setattr(
        master_key_vault_sync,
        "build_sync_azure_credential",
        lambda *, prefer_default_credential=False: credential,
        raising=False,
    )
    monkeypatch.setattr(master_key_vault_sync, "SecretClient", FakeSecretClient)

    client = master_key_vault_sync.build_master_secret_client_sync()

    assert isinstance(client, FakeSecretClient)
    assert created == [("https://master-vault.example", credential)]


def test_build_master_secret_client_sync_can_force_default_credential(monkeypatch) -> None:
    captured: list[bool] = []

    class FakeSecretClient:
        def __init__(self, *, vault_url: str, credential: object) -> None:
            pass

    monkeypatch.setattr(
        master_key_vault_sync.secret_manager_config,
        "AZURE_MASTER_KEY_VAULT_URL",
        "https://master-vault.example",
        raising=False,
    )
    monkeypatch.setattr(
        master_key_vault_sync,
        "build_sync_azure_credential",
        lambda *, prefer_default_credential=False: captured.append(prefer_default_credential)
        or object(),
        raising=False,
    )
    monkeypatch.setattr(master_key_vault_sync, "SecretClient", FakeSecretClient)

    master_key_vault_sync.build_master_secret_client_sync(prefer_default_credential=True)

    assert captured == [True]
