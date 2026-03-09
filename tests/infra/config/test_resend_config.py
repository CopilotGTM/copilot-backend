"""Tests for ResendConfig Key Vault integration."""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
from src.infra.config.configs import ResendConfig


class TestResendConfigEnvVar:
    """When RESEND_API_KEY env var is set, Key Vault is never called."""

    def _make_config(self, api_key: str = "env-key-123") -> ResendConfig:
        config = ResendConfig()
        config._api_key = api_key
        config._credentials_from_env = True
        config._credentials_loaded_at = None
        config._refresh_lock = None
        return config

    @pytest.mark.asyncio
    async def test_returns_env_var_key(self):
        config = self._make_config("my-env-key")
        result = await config.get_api_key()
        assert result == "my-env-key"

    @pytest.mark.asyncio
    async def test_never_calls_vault_when_env_set(self):
        config = self._make_config("my-env-key")
        with patch.object(config, "_load_from_vault", new_callable=AsyncMock) as mock_vault:
            await config.get_api_key()
            await config.get_api_key()
            mock_vault.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_when_env_empty_string(self):
        """If env var was set to empty string, _credentials_from_env is False."""
        config = ResendConfig()
        config._api_key = None
        config._credentials_from_env = False
        config._credentials_loaded_at = None
        config._refresh_lock = None
        # Will try vault, which we mock to return None
        with patch.object(config, "_load_from_vault", new_callable=AsyncMock):
            result = await config.get_api_key()
            assert result is None


class TestResendConfigKeyVault:
    """When env var is NOT set, config fetches from Key Vault with TTL."""

    def _make_config(self) -> ResendConfig:
        config = ResendConfig()
        config._api_key = None
        config._credentials_from_env = False
        config._credentials_loaded_at = None
        config._credentials_ttl_hours = 6.0
        config._refresh_lock = None
        return config

    @pytest.mark.asyncio
    async def test_fetches_from_vault_on_first_call(self):
        config = self._make_config()

        with patch("src.infra.secrets.manager.master_secrets_manager") as mock_manager:
            mock_manager.get = AsyncMock(return_value="vault-key-456")
            await config.get_api_key()

        assert config._api_key == "vault-key-456"
        assert config._credentials_loaded_at is not None

    @pytest.mark.asyncio
    async def test_caches_after_first_fetch(self):
        config = self._make_config()

        with patch("src.infra.secrets.manager.master_secrets_manager") as mock_manager:
            mock_manager.get = AsyncMock(return_value="vault-key-456")
            result1 = await config.get_api_key()
            result2 = await config.get_api_key()
            # Only called once despite two get_api_key() calls
            assert mock_manager.get.call_count == 1

        assert result1 == "vault-key-456"
        assert result2 == "vault-key-456"

    @pytest.mark.asyncio
    async def test_refreshes_after_ttl_expires(self):
        config = self._make_config()
        config._credentials_ttl_hours = 0.0  # Expire immediately

        with patch("src.infra.secrets.manager.master_secrets_manager") as mock_manager:
            mock_manager.get = AsyncMock(side_effect=["key-1", "key-2"])
            result1 = await config.get_api_key()
            # Force expiry
            config._credentials_loaded_at = time.time() - 1
            result2 = await config.get_api_key()

        assert result1 == "key-1"
        assert result2 == "key-2"
        assert mock_manager.get.call_count == 2

    @pytest.mark.asyncio
    async def test_returns_none_when_vault_returns_none(self):
        config = self._make_config()

        with patch("src.infra.secrets.manager.master_secrets_manager") as mock_manager:
            mock_manager.get = AsyncMock(return_value=None)
            result = await config.get_api_key()

        assert result is None

    @pytest.mark.asyncio
    async def test_ttl_not_expired_skips_vault(self):
        config = self._make_config()
        config._api_key = "cached-key"
        config._credentials_loaded_at = time.time()  # Just loaded
        config._credentials_ttl_hours = 6.0

        with patch("src.infra.secrets.manager.master_secrets_manager") as mock_manager:
            mock_manager.get = AsyncMock(return_value="new-key")
            result = await config.get_api_key()

        assert result == "cached-key"
        mock_manager.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_concurrent_calls_only_fetch_once(self):
        """Double-checked locking prevents multiple vault fetches."""
        config = self._make_config()

        call_count = 0

        async def slow_get(name: str) -> str:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return "vault-key"

        with patch("src.infra.secrets.manager.master_secrets_manager") as mock_manager:
            mock_manager.get = slow_get
            results = await asyncio.gather(
                config.get_api_key(),
                config.get_api_key(),
                config.get_api_key(),
            )

        assert all(r == "vault-key" for r in results)
        assert call_count == 1
