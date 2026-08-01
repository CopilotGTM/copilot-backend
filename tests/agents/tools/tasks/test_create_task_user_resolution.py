"""Regression contracts for canonical task-assignee resolution."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from src.agents.tools.tasks.create_task import _resolve_deal_by_name, _resolve_user_by_name
from src.service.crm.access.crm_user_directory_service import CRMUserDirectoryResult

ORG_ID = UUID("11111111-1111-1111-1111-111111111111")


class _Context:
    organisation_id = ORG_ID

    @asynccontextmanager
    async def acquire_session(self):  # noqa: ANN201
        """Yield the org session used by the shared directory."""
        yield MagicMock()


def _user(name: str = "Jonathan Mehta") -> SimpleNamespace:
    first_name, last_name = name.split(" ", 1)
    return SimpleNamespace(
        user_id=uuid4(),
        full_name=name,
        first_name=first_name,
        last_name=last_name,
        email="jonathan@example.com",
    )


@pytest.mark.asyncio
async def test_resolve_user_by_name_accepts_one_canonical_name_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exact unique directory result may safely become a task assignee ID."""
    target = _user()
    search = AsyncMock(
        return_value=CRMUserDirectoryResult(
            status="resolved",
            candidates=[target],
            match_type="exact",
        )
    )
    directory = MagicMock()
    directory.return_value.search = search
    monkeypatch.setattr(
        "src.service.crm.access.crm_user_directory_service.CRMUserDirectoryService",
        directory,
    )

    result = await _resolve_user_by_name(_Context(), "Jonathan Mehta")

    assert result == (target.user_id, "Jonathan Mehta")
    assert directory.call_args.args[1] == ORG_ID
    search.assert_awaited_once_with("Jonathan Mehta")


@pytest.mark.asyncio
async def test_resolve_user_by_name_rejects_one_fuzzy_name_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A unique fuzzy name remains a suggestion and cannot authorize a task write."""
    target = _user()
    search = AsyncMock(
        return_value=CRMUserDirectoryResult(
            status="resolved",
            candidates=[target],
            match_type="fuzzy_name",
        )
    )
    directory = MagicMock()
    directory.return_value.search = search
    monkeypatch.setattr(
        "src.service.crm.access.crm_user_directory_service.CRMUserDirectoryService",
        directory,
    )

    assert await _resolve_user_by_name(_Context(), "Jonatan Mehta") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("match_type", ["partial", "fuzzy_email"])
async def test_resolve_user_by_name_rejects_ambiguous_or_suggested_identity(
    monkeypatch: pytest.MonkeyPatch,
    match_type: str,
) -> None:
    """A write path must not pick the first person or a close email suggestion."""
    users = [_user("Jonathan Mehta"), _user("Jonathan Menon")]
    search = AsyncMock(
        return_value=CRMUserDirectoryResult(
            status="ambiguous",
            candidates=users,
            match_type=match_type,  # type: ignore[arg-type]
        )
    )
    directory = MagicMock()
    directory.return_value.search = search
    monkeypatch.setattr(
        "src.service.crm.access.crm_user_directory_service.CRMUserDirectoryService",
        directory,
    )

    assert await _resolve_user_by_name(_Context(), "jonthan@example.com") is None


@pytest.mark.asyncio
async def test_resolve_deal_by_name_rejects_one_fuzzy_search_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A unique fuzzy deal remains a suggestion and cannot authorize task linking."""
    deal = SimpleNamespace(deal_id=uuid4(), name="Northern Star Renewal")
    search = AsyncMock(return_value=[{"deal": deal, "match": {"type": "fuzzy"}}])
    service = MagicMock()
    service.return_value.search_deals_for_agent = search
    monkeypatch.setattr(
        "src.agents.tools.tasks.create_task.resolve_agent_access_actor",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr("src.service.crm.deals.read_service.DealReadService", service)

    assert await _resolve_deal_by_name(_Context(), "Nothern Star Renewal") is None
    search.assert_awaited_once_with(q="Nothern Star Renewal", limit=5)


@pytest.mark.asyncio
async def test_resolve_deal_by_name_accepts_one_exact_search_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One exact accessible deal may safely become the task's related record."""
    deal = SimpleNamespace(deal_id=uuid4(), name="Northern Star Renewal")
    search = AsyncMock(return_value=[{"deal": deal, "match": {"type": "exact"}}])
    service = MagicMock()
    service.return_value.search_deals_for_agent = search
    monkeypatch.setattr(
        "src.agents.tools.tasks.create_task.resolve_agent_access_actor",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr("src.service.crm.deals.read_service.DealReadService", service)

    result = await _resolve_deal_by_name(_Context(), "Northern Star Renewal")

    assert result == (deal.deal_id, "Northern Star Renewal")
