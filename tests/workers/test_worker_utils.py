"""Tests for public-schema context loaded by background workers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.workers.base.worker_utils import get_organisation_briefing_context


@pytest.mark.asyncio
async def test_get_organisation_briefing_context_returns_configured_fields() -> None:
    """Meeting prep receives the authoritative seller identity and websites together."""
    row = SimpleNamespace(
        organisation_name="Example Seller",
        website_urls=["https://seller.example"],
        summary="Seller summary",
    )
    result = SimpleNamespace(one_or_none=lambda: row)
    session = SimpleNamespace(execute=AsyncMock(return_value=result))

    context = await get_organisation_briefing_context(session, uuid4())

    assert context == (
        "Example Seller",
        ["https://seller.example"],
        "Seller summary",
    )


@pytest.mark.asyncio
async def test_get_organisation_briefing_context_handles_missing_record() -> None:
    """An absent optional seller profile must not block briefing generation."""
    result = SimpleNamespace(one_or_none=lambda: None)
    session = SimpleNamespace(execute=AsyncMock(return_value=result))

    context = await get_organisation_briefing_context(session, uuid4())

    assert context == (None, [], None)
