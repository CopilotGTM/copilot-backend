"""Contracts for the channel-neutral chat attachment reader."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_reader_returns_typed_not_found_without_attachment_context() -> None:
    """Callers receive a neutral result instead of an agent-tool response."""
    module = importlib.import_module("src.service.chat.chat_attachment_reader")
    context = SimpleNamespace(
        organisation_id=uuid4(),
        user_id=uuid4(),
        metadata={},
    )

    result = await module.read_attachment_content(
        str(uuid4()),
        context=context,
        max_chars=100,
    )

    assert result.kind == "unavailable"
    assert result.error_code == "not_found"
    assert result.content_bytes is None
