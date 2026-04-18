from __future__ import annotations

from importlib import reload


def test_secret_manager_config_uses_higher_default_timeout(monkeypatch) -> None:
    monkeypatch.delenv("SECRET_GET_TIMEOUT_SEC", raising=False)

    import src.infra.config.configs as configs_runtime

    reload(configs_runtime)

    assert configs_runtime.secret_manager_config.SECRET_GET_TIMEOUT_SEC == 20


def test_secrets_manager_uses_config_default_retry_settings(monkeypatch) -> None:
    import src.infra.secrets.manager as secrets_manager_runtime

    monkeypatch.setattr(
        secrets_manager_runtime.secret_manager_config,
        "SECRET_GET_MAX_RETRIES",
        2,
        raising=False,
    )
    monkeypatch.setattr(
        secrets_manager_runtime.secret_manager_config,
        "SECRET_GET_RETRY_INITIAL_DELAY_SEC",
        0.5,
        raising=False,
    )
    monkeypatch.setattr(
        secrets_manager_runtime.secret_manager_config,
        "SECRET_GET_RETRY_MAX_DELAY_SEC",
        4.0,
        raising=False,
    )

    manager = secrets_manager_runtime.SecretsManager(provider=object())

    assert manager.max_retries == 2
    assert manager.initial_retry_delay == 0.5
    assert manager.max_retry_delay == 4.0
