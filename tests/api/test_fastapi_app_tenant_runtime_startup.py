from __future__ import annotations

import pytest
from src.api import fastapi_app


def _patch_lifespan_external_startup(monkeypatch) -> None:
    monkeypatch.setattr(fastapi_app, "initialize_redis_pool", lambda: None)

    async def async_noop(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        return None

    monkeypatch.setattr(fastapi_app, "initialize_async_redis_pool", async_noop)
    monkeypatch.setattr(fastapi_app.security_secrets_config, "preload_required", async_noop)
    monkeypatch.setattr(
        "src.infra.auth.supabase_admin_client.supabase_admin_client.initialize",
        async_noop,
    )
    monkeypatch.setattr(
        "src.infra.auth.supabase_user_client.supabase_user_client.initialize",
        async_noop,
    )
    monkeypatch.setattr(
        "src.infra.security.token_revocation.token_revocation_service.initialize",
        async_noop,
    )
    monkeypatch.setattr(fastapi_app, "init_sentry", lambda: None)
    monkeypatch.setattr(fastapi_app, "async_init_otel", lambda service_name_suffix: async_noop())


@pytest.mark.asyncio
async def test_api_lifespan_validates_shared_tenant_runtime_before_serving(
    monkeypatch,
) -> None:
    calls: list[str] = []

    _patch_lifespan_external_startup(monkeypatch)

    def validate() -> None:
        calls.append("validated")
        raise RuntimeError("TENANT_DATABASE_USER is required")

    monkeypatch.setattr(fastapi_app, "validate_current_shared_tenant_runtime_startup", validate)

    with pytest.raises(RuntimeError, match="TENANT_DATABASE_USER"):
        async with fastapi_app.lifespan(object()):
            pass

    assert calls == ["validated"]


@pytest.mark.asyncio
async def test_api_lifespan_validates_shared_tenant_runtime_password_before_serving(
    monkeypatch,
) -> None:
    calls: list[str] = []

    _patch_lifespan_external_startup(monkeypatch)
    monkeypatch.setattr(fastapi_app, "validate_current_shared_tenant_runtime_startup", lambda: None)

    async def validate_password(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        calls.append("password")
        raise RuntimeError("Tenant runtime password secret unavailable")

    monkeypatch.setattr(
        fastapi_app,
        "validate_current_shared_tenant_runtime_password",
        validate_password,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="runtime password secret"):
        async with fastapi_app.lifespan(object()):
            pass

    assert calls == ["password"]
