from __future__ import annotations

import pytest
from src.workers.base import celery_base


def test_worker_runtime_validation_raises_shared_tenant_config_error(monkeypatch) -> None:
    def validate() -> None:
        raise RuntimeError("TENANT_DATABASE_USER is required")

    monkeypatch.setattr(celery_base, "validate_current_shared_tenant_runtime_startup", validate)

    with pytest.raises(RuntimeError, match="TENANT_DATABASE_USER"):
        celery_base._validate_tenant_runtime_startup()


def test_worker_runtime_validation_checks_shared_tenant_password(monkeypatch) -> None:
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
