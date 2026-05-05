from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from src.agents.tools.tasks.create_task import _resolve_user_by_name

ORG_ID = UUID("11111111-1111-1111-1111-111111111111")
OTHER_ORG_ID = UUID("99999999-9999-9999-9999-999999999999")


class _Result:
    def __init__(self, users: list[SimpleNamespace]) -> None:
        self._users = users

    def scalars(self) -> _Result:
        return self

    def first(self) -> SimpleNamespace | None:
        return self._users[0] if self._users else None

    def all(self) -> list[SimpleNamespace]:
        return self._users


class _Session:
    def __init__(self, users: list[SimpleNamespace]) -> None:
        self.users = users

    async def execute(self, stmt: object) -> _Result:
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
        if "wrong-org@example.com" in compiled:
            return _Result([user for user in self.users if user.email == "wrong-org@example.com"])
        if "@" in compiled:
            return _Result([user for user in self.users if user.email.lower() in compiled])
        if "concat" not in compiled:
            return _Result([])
        return _Result(self.users)


def _user(
    first_name: str,
    last_name: str,
    email: str,
    *,
    organisation_id: UUID = ORG_ID,
) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=UUID(int=abs(hash(email)) % (1 << 128)),
        organisation_id=organisation_id,
        first_name=first_name,
        last_name=last_name,
        email=email,
    )


def _session_scope(users: list[SimpleNamespace]):
    async def scope():
        yield _Session(users)

    return scope()


@pytest.mark.asyncio
async def test_resolve_user_by_name_matches_exact_full_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _user("Nikhil", "Singhal", "nikhil@copilotgtm.com")

    monkeypatch.setattr(
        "src.infra.database.sessions.acquire_master_async_session",
        lambda: _session_scope([target]),
    )

    result = await _resolve_user_by_name(ORG_ID, "Nikhil Singhal")

    assert result == (target.user_id, "Nikhil Singhal")


@pytest.mark.asyncio
async def test_resolve_user_by_name_matches_case_insensitive_full_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _user("Nikhil", "Singhal", "nikhil@copilotgtm.com")

    monkeypatch.setattr(
        "src.infra.database.sessions.acquire_master_async_session",
        lambda: _session_scope([target]),
    )

    result = await _resolve_user_by_name(ORG_ID, "nikhil singhal")

    assert result == (target.user_id, "Nikhil Singhal")


@pytest.mark.asyncio
async def test_resolve_user_by_name_returns_none_for_ambiguous_partial_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users = [
        _user("Nikhil", "Singhal", "nikhil@copilotgtm.com"),
        _user("Nikhil", "Shah", "nikhil.shah@copilotgtm.com"),
    ]

    monkeypatch.setattr(
        "src.infra.database.sessions.acquire_master_async_session",
        lambda: _session_scope(users),
    )

    assert await _resolve_user_by_name(ORG_ID, "Nikhil") is None


@pytest.mark.asyncio
async def test_resolve_user_by_name_never_returns_cross_org_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong_org_user = _user(
        "Nikhil",
        "Singhal",
        "wrong-org@example.com",
        organisation_id=OTHER_ORG_ID,
    )

    monkeypatch.setattr(
        "src.infra.database.sessions.acquire_master_async_session",
        lambda: _session_scope([wrong_org_user]),
    )

    assert await _resolve_user_by_name(ORG_ID, "wrong-org@example.com") is None
