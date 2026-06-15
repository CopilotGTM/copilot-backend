from __future__ import annotations

import pytest
from scripts.org_admin_session import AdminAsyncSessionAdapter


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
