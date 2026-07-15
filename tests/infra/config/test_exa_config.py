"""Credential-loading contracts for application-level Exa search."""

from unittest.mock import AsyncMock, patch

import pytest
from src.infra.config.configs import ExaConfig


def _config_without_environment_key() -> ExaConfig:
    config = ExaConfig()
    config._api_key = None
    config._credentials_from_env = False
    config._credentials_loaded_at = None
    config._refresh_lock = None
    return config


@pytest.mark.asyncio
async def test_environment_key_skips_key_vault() -> None:
    """An explicit local key prevents Azure Key Vault access."""
    # Local credentials are explicit overrides, so provider calls must not make
    # local development depend on Azure identity or Key Vault availability.
    config = ExaConfig()
    config._api_key = "env-key"
    config._credentials_from_env = True
    config._credentials_loaded_at = None

    with patch.object(config, "_load_from_vault", new_callable=AsyncMock) as load:
        assert await config.get_api_key() == "env-key"
    load.assert_not_awaited()


@pytest.mark.asyncio
async def test_key_vault_value_is_cached() -> None:
    """A deployed worker reuses its vault key for the configured TTL."""
    # Deployed agents may search repeatedly in one run; only the first call
    # should pay the master-vault network round trip during the configured TTL.
    config = _config_without_environment_key()

    with patch("src.infra.secrets.manager.master_secrets_manager") as manager:
        manager.get = AsyncMock(return_value="vault-key")
        first = await config.get_api_key()
        second = await config.get_api_key()

    assert first == second == "vault-key"
    manager.get.assert_awaited_once_with("exa-api-key")


@pytest.mark.asyncio
async def test_rejected_vault_key_refresh_is_shared() -> None:
    """Concurrent-style stale-key refreshes reuse the first replacement."""
    # The second caller presents the old key after the first caller has already
    # refreshed it, so it must not trigger another provider-secret lookup.
    config = _config_without_environment_key()
    config._api_key = "old-key"

    with patch("src.infra.secrets.manager.master_secrets_manager") as manager:
        manager.get = AsyncMock(return_value="new-key")
        first = await config.refresh_rejected_api_key("old-key")
        second = await config.refresh_rejected_api_key("old-key")

    assert first == second == "new-key"
    manager.get.assert_awaited_once_with("exa-api-key")


@pytest.mark.asyncio
async def test_environment_key_is_not_replaced_after_rejection() -> None:
    """An invalid explicit environment key remains an operator-owned failure."""
    # Falling through to Key Vault would violate the documented precedence and
    # make local failures depend on whichever production secret is accessible.
    config = ExaConfig()
    config._api_key = "env-key"
    config._credentials_from_env = True

    with patch.object(config, "_load_from_vault", new_callable=AsyncMock) as load:
        assert await config.refresh_rejected_api_key("env-key") == "env-key"
    load.assert_not_awaited()
