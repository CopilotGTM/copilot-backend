from __future__ import annotations

from uuid import UUID

import pytest
import scripts.org_admin_session as org_admin_session
from scripts.org_admin_session import AdminAsyncSessionAdapter
from src.infra.database.tenant_search_path import (
    TENANT_DATA_SCHEMA,
    TENANT_ORGANISATION_ID_SESSION_KEY,
    TENANT_SCHEMA_SESSION_KEY,
    TENANT_SEARCH_PATH_SESSION_KEY,
    TRUSTED_EXTENSION_SCHEMA,
)


class _NoAutoflushContext:
    def __init__(self, session: _FakeSyncSession) -> None:
        self.session = session

    def __enter__(self) -> None:
        self.session.inside_no_autoflush = True

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        self.session.inside_no_autoflush = False


class _FakeSyncSession:
    def __init__(self) -> None:
        self.inside_no_autoflush = False
        self.execute_used_no_autoflush = False

    @property
    def no_autoflush(self) -> _NoAutoflushContext:
        return _NoAutoflushContext(self)

    def execute(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        self.execute_used_no_autoflush = self.inside_no_autoflush
        return "result"


@pytest.mark.asyncio
async def test_admin_async_session_execute_disables_autoflush() -> None:
    sync_session = _FakeSyncSession()
    adapter = AdminAsyncSessionAdapter(sync_session)  # type: ignore[arg-type]

    result = await adapter.execute("SELECT 1")

    assert result == "result"
    assert sync_session.execute_used_no_autoflush is True


@pytest.mark.asyncio
async def test_admin_org_session_uses_shared_schema_and_tenant_context(monkeypatch) -> None:
    organisation_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    class FakeEngine:
        def dispose(self) -> None:
            pass

    class FakeSession:
        def __init__(self) -> None:
            self.info: dict[str, object] = {}

        def close(self) -> None:
            pass

    session = FakeSession()
    monkeypatch.setattr(org_admin_session, "create_engine", lambda *_args, **_kwargs: FakeEngine())
    monkeypatch.setattr(org_admin_session, "Session", lambda *_args, **_kwargs: session)

    async with org_admin_session.acquire_org_admin_async_session(organisation_id):
        assert session.info[TENANT_ORGANISATION_ID_SESSION_KEY] == organisation_id
        assert session.info[TENANT_SCHEMA_SESSION_KEY] == TENANT_DATA_SCHEMA
        assert session.info[TENANT_SEARCH_PATH_SESSION_KEY] == (
            TENANT_DATA_SCHEMA,
            TRUSTED_EXTENSION_SCHEMA,
            "public",
        )
