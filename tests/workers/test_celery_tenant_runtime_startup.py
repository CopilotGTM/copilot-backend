from __future__ import annotations

import pytest
from src.workers.base import celery_base


def test_worker_runtime_validation_raises_shared_tenant_config_error(monkeypatch) -> None:
    def validate() -> None:
        raise RuntimeError("TENANT_DATABASE_USER is required")

    monkeypatch.setattr(celery_base, "validate_current_shared_tenant_runtime_startup", validate)

    with pytest.raises(RuntimeError, match="TENANT_DATABASE_USER"):
        celery_base._validate_tenant_runtime_config_startup()


def test_worker_main_process_runtime_validation_checks_shared_tenant_password(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(celery_base, "validate_current_shared_tenant_runtime_startup", lambda: None)

    def validate_password(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        calls.append("password")
        raise RuntimeError("Tenant runtime password secret unavailable")

    monkeypatch.setattr(
        celery_base,
        "validate_current_shared_tenant_runtime_password_sync",
        validate_password,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="runtime password secret"):
        celery_base._validate_tenant_runtime_startup()

    assert calls == ["password"]


def test_worker_fork_validation_does_not_call_key_vault(monkeypatch) -> None:
    calls: list[str] = []

    def validate_config() -> None:
        calls.append("config")

    def validate_password(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise AssertionError("fork init must not call Key Vault")

    monkeypatch.setattr(
        celery_base,
        "validate_current_shared_tenant_runtime_startup",
        validate_config,
    )
    monkeypatch.setattr(
        celery_base,
        "validate_current_shared_tenant_runtime_password_sync",
        validate_password,
    )

    celery_base._validate_tenant_runtime_config_startup()

    assert calls == ["config"]
