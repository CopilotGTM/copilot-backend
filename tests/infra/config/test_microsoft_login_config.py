"""Contracts for keeping Microsoft login credentials separate from integrations."""

from unittest.mock import AsyncMock, patch

import pytest
from src.infra.config.configs import MicrosoftLoginConfig


@pytest.mark.asyncio
async def test_dedicated_login_environment_credentials_take_precedence(monkeypatch) -> None:
    """Local setup can supply the dedicated login pair without reading Key Vault."""
    monkeypatch.setenv("MICROSOFT_LOGIN_CLIENT_ID", "login-client-id")
    monkeypatch.setenv("MICROSOFT_LOGIN_CLIENT_SECRET", "login-client-secret")
    config = MicrosoftLoginConfig()

    with patch(
        "src.infra.secrets.manager.master_secrets_manager.get",
        new=AsyncMock(),
    ) as get_secret:
        credentials = await config.get_credentials()

    assert credentials == ("login-client-id", "login-client-secret")
    get_secret.assert_not_awaited()


@pytest.mark.asyncio
async def test_dedicated_login_config_uses_separate_key_vault_names() -> None:
    """Shared environments read login-specific secrets from the master vault."""
    config = MicrosoftLoginConfig()

    with patch(
        "src.infra.secrets.manager.master_secrets_manager.get",
        new=AsyncMock(
            side_effect=lambda name: {
                "microsoft-login-client-id": "login-client-id",
                "microsoft-login-client-secret": "login-client-secret",
            }[name]
        ),
    ) as get_secret:
        credentials = await config.get_credentials()

    assert credentials == ("login-client-id", "login-client-secret")
    assert [call.args[0] for call in get_secret.await_args_list] == [
        "microsoft-login-client-id",
        "microsoft-login-client-secret",
    ]


@pytest.mark.asyncio
async def test_partial_dedicated_login_environment_pair_is_rejected(monkeypatch) -> None:
    """A partial pair must not produce an unusable OAuth configuration."""
    monkeypatch.setenv("MICROSOFT_LOGIN_CLIENT_ID", "login-client-id")
    monkeypatch.delenv("MICROSOFT_LOGIN_CLIENT_SECRET", raising=False)
    config = MicrosoftLoginConfig()

    with pytest.raises(RuntimeError, match="must be set together"):
        await config.get_credentials()
