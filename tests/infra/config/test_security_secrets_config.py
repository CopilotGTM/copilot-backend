"""Tests for runtime signing secret loading from env vars or Key Vault."""

from unittest.mock import AsyncMock, patch

import pytest
from src.infra.config.configs import SecuritySecretsConfig


def _make_config() -> SecuritySecretsConfig:
    config = SecuritySecretsConfig()
    config._session_secret = ""
    config._oauth_state_secret = ""
    config._voice_realtime_config_secret = ""
    config._session_secret_from_env = False
    config._oauth_state_secret_from_env = False
    config._voice_realtime_config_secret_from_env = False
    config._credentials_loaded_at = None
    config._credentials_ttl_hours = 6.0
    config._refresh_lock = None
    return config


@pytest.mark.asyncio
async def test_preload_uses_env_overrides_without_vault_calls() -> None:
    config = _make_config()
    config._session_secret = "env-session"
    config._oauth_state_secret = "env-oauth"
    config._voice_realtime_config_secret = "env-voice"
    config._session_secret_from_env = True
    config._oauth_state_secret_from_env = True
    config._voice_realtime_config_secret_from_env = True

    with patch("src.infra.secrets.manager.master_secrets_manager") as mock_manager:
        mock_manager.get = AsyncMock(return_value="should-not-be-used")
        await config.preload_required(include_voice_realtime=True)

    assert await config.get_session_secret() == "env-session"
    assert await config.get_oauth_state_secret() == "env-oauth"
    assert await config.get_voice_realtime_config_secret() == "env-voice"
    mock_manager.get.assert_not_called()


def test_get_session_secret_sync_fetches_from_key_vault() -> None:
    config = _make_config()

    async def fake_get(name: str) -> str | None:
        mapping = {
            config.SECRET_NAME_SESSION_SECRET: "vault-session",
            config.SECRET_NAME_OAUTH_STATE_SECRET: "vault-oauth",
        }
        return mapping.get(name)

    with patch("src.infra.secrets.manager.master_secrets_manager") as mock_manager:
        mock_manager.get = fake_get
        result = config.get_session_secret_sync()

    assert result == "vault-session"
    assert config._oauth_state_secret == "vault-oauth"


@pytest.mark.asyncio
async def test_get_oauth_state_secret_raises_when_key_vault_secret_missing() -> None:
    config = _make_config()

    async def fake_get(_name: str) -> str | None:
        return None

    with patch("src.infra.secrets.manager.master_secrets_manager") as mock_manager:
        mock_manager.get = fake_get
        with pytest.raises(RuntimeError, match="SECRET_KEY unavailable"):
            await config.get_oauth_state_secret()
