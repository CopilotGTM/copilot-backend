"""Contracts for the general agent's organisation-scoped CRM-user search tool."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from src.agents.tools.crm import search_crm_users as search_crm_users_module


@pytest.mark.asyncio
async def test_tool_reports_lookup_failure_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operational failure must not be presented as evidence that a person is absent."""
    monkeypatch.setattr(
        search_crm_users_module,
        "search_crm_users",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    tool = search_crm_users_module.create_search_crm_users(MagicMock(), lambda: None)

    result = await tool("Manish")

    assert result == {
        "status": "error",
        "message": "Could not search team members. Please try again.",
        "candidates": [],
    }


@pytest.mark.asyncio
async def test_tool_preserves_successful_not_found_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed lookup with zero matches remains eligible for the email retry flow."""
    monkeypatch.setattr(
        search_crm_users_module,
        "search_crm_users",
        AsyncMock(return_value={"status": "not_found", "candidates": []}),
    )
    tool = search_crm_users_module.create_search_crm_users(MagicMock(), lambda: None)

    result = await tool("unknown@example.com")

    assert result == {"status": "not_found", "candidates": []}
