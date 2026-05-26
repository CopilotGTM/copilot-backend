"""Tests for SalesforceIntegrationConfig credential loading behavior."""

from unittest.mock import AsyncMock, patch

import pytest
from src.infra.config.configs import SalesforceIntegrationConfig


class TestSalesforceIntegrationConfigEnvVar:
    """When Salesforce credentials are set directly, Key Vault is skipped."""

    def _make_config(
        self,
        client_id: str = "salesforce-client-id",
        client_secret: str = "salesforce-client-secret",
    ) -> SalesforceIntegrationConfig:
        config = SalesforceIntegrationConfig()
        config._client_id = client_id
        config._client_secret = client_secret
        config._credentials_from_env = True
        config._credentials_loaded_at = None
        config._refresh_lock = None
        return config

    @pytest.mark.asyncio
    async def test_returns_env_credentials(self) -> None:
        config = self._make_config("env-id", "env-secret")

        result = await config.get_credentials()

        assert result == ("env-id", "env-secret")

    @pytest.mark.asyncio
    async def test_never_calls_vault_when_env_set(self) -> None:
        config = self._make_config()

        with patch.object(config, "_load_from_vault", new_callable=AsyncMock) as mock_vault:
            await config.get_credentials()
            await config.get_credentials()

        mock_vault.assert_not_called()


class TestSalesforceExternalClientAppInstallUrl:
    """Salesforce package install URL is non-secret runtime config."""

    def test_defaults_to_empty_install_url_when_env_var_absent(self, monkeypatch) -> None:
        monkeypatch.delenv("SALESFORCE_EXTERNAL_CLIENT_APP_INSTALL_URL", raising=False)

        config = SalesforceIntegrationConfig()

        assert config.EXTERNAL_CLIENT_APP_INSTALL_URL == ""

    def test_reads_install_url_from_env(self, monkeypatch) -> None:
        install_url = "https://test.salesforce.com/packaging/installPackage.apexp?p0=04td"
        monkeypatch.setenv("SALESFORCE_EXTERNAL_CLIENT_APP_INSTALL_URL", install_url)

        config = SalesforceIntegrationConfig()

        assert install_url == config.EXTERNAL_CLIENT_APP_INSTALL_URL
