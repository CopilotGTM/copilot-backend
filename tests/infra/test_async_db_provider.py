from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import src.infra.database.provider_async as provider_async


def test_master_engine_uses_explicit_pool_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_async_engine(url, **kwargs):  # noqa: ANN001
        captured["url"] = url
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(
        provider_async.database_config,
        "DATABASE_POOL_TIMEOUT_SEC",
        60,
        raising=False,
    )
    monkeypatch.setattr(provider_async, "create_async_engine", fake_create_async_engine)

    provider = provider_async.AsyncSqlAlchemyProvider()
    provider._get_master_engine()

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["pool_timeout"] == 60


@pytest.mark.asyncio
async def test_tenant_engine_uses_explicit_pool_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_async_engine(url, **kwargs):  # noqa: ANN001
        captured["url"] = url
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(
        provider_async.database_config,
        "DATABASE_POOL_TIMEOUT_SEC",
        60,
        raising=False,
    )
    monkeypatch.setattr(provider_async, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(
        provider_async.secrets_manager,
        "get_with_fallback",
        AsyncMock(return_value="tenant-secret"),
    )

    provider = provider_async.AsyncSqlAlchemyProvider()
    await provider._get_tenant_engine(uuid4())

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["pool_timeout"] == 60
