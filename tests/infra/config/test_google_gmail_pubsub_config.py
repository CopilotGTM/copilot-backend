"""Tests for Gmail Pub/Sub webhook token configuration."""

import time
from unittest.mock import AsyncMock, patch

import pytest
from src.infra.config.configs import GoogleGmailPubSubConfig


class TestGoogleGmailPubSubConfig:
    """Gmail push auth can come from env locally or Key Vault in deployments."""

    def _make_config(self) -> GoogleGmailPubSubConfig:
        config = GoogleGmailPubSubConfig()
        config.SECRET_NAME_PUSH_TOKEN = "google-gmail-pubsub-push-token"  # pragma: allowlist secret
        config._push_token = None
        config._token_from_env = False
        config._credentials_ttl_hours = 6.0
        config._credentials_loaded_at = None
        config._refresh_lock = None
        return config

    @pytest.mark.asyncio
    async def test_live_env_token_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("GOOGLE_GMAIL_PUBSUB_PUSH_TOKEN", "env-token")
        config = self._make_config()

        with patch.object(config, "_load_from_vault", new_callable=AsyncMock) as load_from_vault:
            result = await config.get_push_token()

        assert result == "env-token"
        load_from_vault.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetches_push_token_from_key_vault(self, monkeypatch) -> None:
        monkeypatch.delenv("GOOGLE_GMAIL_PUBSUB_PUSH_TOKEN", raising=False)
        config = self._make_config()

        with patch("src.infra.secrets.manager.master_secrets_manager") as mock_manager:
            mock_manager.get = AsyncMock(return_value="vault-token")
            result = await config.get_push_token()

        assert result == "vault-token"
        mock_manager.get.assert_awaited_once_with("google-gmail-pubsub-push-token")

    @pytest.mark.asyncio
    async def test_cached_token_skips_key_vault_before_ttl(self, monkeypatch) -> None:
        monkeypatch.delenv("GOOGLE_GMAIL_PUBSUB_PUSH_TOKEN", raising=False)
        config = self._make_config()
        config._push_token = "cached-token"
        config._credentials_loaded_at = time.time()

        with patch("src.infra.secrets.manager.master_secrets_manager") as mock_manager:
            mock_manager.get = AsyncMock(return_value="new-token")
            result = await config.get_push_token()

        assert result == "cached-token"
        mock_manager.get.assert_not_called()
