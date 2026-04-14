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
