"""Celery startup contracts for shared-tenant configuration and connections."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from celery.signals import worker_init
from src.infra.database import factory
from src.workers.base import celery_base


def test_worker_runtime_validation_raises_shared_tenant_config_error(monkeypatch) -> None:
    def validate() -> None:
        raise RuntimeError("TENANT_DATABASE_USER is required")

    monkeypatch.setattr(celery_base, "validate_current_shared_tenant_runtime_startup", validate)

    with pytest.raises(RuntimeError, match="TENANT_DATABASE_USER"):
        celery_base._validate_tenant_runtime_config_startup()


def test_worker_main_process_runtime_validation_checks_shared_tenant_password(monkeypatch) -> None:
    """Failed parent validation must release any pool it opened before raising."""

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
    close_mock = Mock()
    monkeypatch.setattr(
        factory,
        "close_sync_database_provider",
        close_mock,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="runtime password secret"):
        celery_base._validate_tenant_runtime_startup()

    assert calls == ["password"]
    close_mock.assert_called_once_with()


def test_worker_main_process_releases_validation_pools_on_success(monkeypatch) -> None:
    """Readiness-only connections must not remain open in the Celery parent."""

    monkeypatch.setattr(
        celery_base,
        "validate_current_shared_tenant_runtime_startup",
        lambda: None,
    )
    monkeypatch.setattr(
        celery_base,
        "validate_current_shared_tenant_runtime_password_sync",
        lambda **kwargs: None,
    )
    close_mock = Mock()
    monkeypatch.setattr(
        factory,
        "close_sync_database_provider",
        close_mock,
        raising=False,
    )

    celery_base._validate_tenant_runtime_startup()

    close_mock.assert_called_once_with()


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


def test_worker_parent_exits_when_tenant_runtime_validation_fails(monkeypatch) -> None:
    """A worker must terminate before consuming jobs when its tenant gate fails."""

    def fail_validation() -> None:
        raise RuntimeError("tenant catalog is unsafe")

    monkeypatch.setattr(celery_base, "_validate_tenant_runtime_startup", fail_validation)
    celery_base._register_observability_signals()

    with pytest.raises(SystemExit) as error:
        worker_init.send(sender=None)

    assert error.value.code == 1
    assert isinstance(error.value.__cause__, RuntimeError)
