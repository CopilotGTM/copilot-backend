"""Contracts for keeping Google login credentials separate from integrations."""

from unittest.mock import AsyncMock, patch

import pytest
from src.infra.config.configs import GoogleIntegrationConfig, GoogleLoginConfig


@pytest.mark.asyncio
async def test_login_config_can_reuse_integration_credentials_during_rollout() -> None:
    """Development can alias values without coupling login to integration names."""
    integration_config = GoogleIntegrationConfig()
    integration_config.get_credentials = AsyncMock(
        return_value=("shared-client-id", "shared-client-secret")
    )
    config = GoogleLoginConfig(
        integration_config,
        use_integration_credentials=True,
    )

    assert await config.get_credentials() == (
        "shared-client-id",
        "shared-client-secret",
    )


@pytest.mark.asyncio
async def test_dedicated_login_environment_credentials_take_precedence(monkeypatch) -> None:
    """A dedicated pair switches login without changing integration credentials."""
    monkeypatch.setenv("GOOGLE_LOGIN_CLIENT_ID", "login-client-id")
    monkeypatch.setenv("GOOGLE_LOGIN_CLIENT_SECRET", "login-client-secret")
    integration_config = GoogleIntegrationConfig()
    integration_config.get_credentials = AsyncMock()
    config = GoogleLoginConfig(
        integration_config,
        use_integration_credentials=True,
    )

    assert await config.get_credentials() == (
        "login-client-id",
        "login-client-secret",
    )
    integration_config.get_credentials.assert_not_awaited()


@pytest.mark.asyncio
async def test_dedicated_login_config_uses_separate_key_vault_names() -> None:
    """Shared environments read login-specific Key Vault secret names."""
    config = GoogleLoginConfig(
        GoogleIntegrationConfig(),
        use_integration_credentials=False,
    )

    with patch(
        "src.infra.secrets.manager.master_secrets_manager.get",
        new=AsyncMock(
            side_effect=lambda name: {
                "google-login-client-id": "login-client-id",
                "google-login-client-secret": "login-client-secret",
            }[name]
        ),
    ) as get_secret:
        credentials = await config.get_credentials()

    assert credentials == ("login-client-id", "login-client-secret")
    assert [call.args[0] for call in get_secret.await_args_list] == [
        "google-login-client-id",
        "google-login-client-secret",
    ]


@pytest.mark.asyncio
async def test_partial_dedicated_login_environment_pair_is_rejected(monkeypatch) -> None:
    """A partial pair must not produce a mixed-client OAuth configuration."""
    monkeypatch.setenv("GOOGLE_LOGIN_CLIENT_ID", "login-client-id")
    monkeypatch.delenv("GOOGLE_LOGIN_CLIENT_SECRET", raising=False)
    config = GoogleLoginConfig(
        GoogleIntegrationConfig(),
        use_integration_credentials=True,
    )

    with pytest.raises(RuntimeError, match="must be set together"):
        await config.get_credentials()


def test_production_defaults_to_dedicated_login_credentials(monkeypatch) -> None:
    """Production defaults away from the temporary integration alias."""
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.delenv("GOOGLE_LOGIN_USE_INTEGRATION_CREDENTIALS", raising=False)

    config = GoogleLoginConfig(GoogleIntegrationConfig())

    assert config._use_integration_credentials is False


def test_production_rejects_integration_credential_fallback(monkeypatch) -> None:
    """Production cannot explicitly weaken the credential isolation boundary."""
    monkeypatch.setenv("ENV", "prod")

    with pytest.raises(RuntimeError, match="must use dedicated credentials"):
        GoogleLoginConfig(
            GoogleIntegrationConfig(),
            use_integration_credentials=True,
        )
