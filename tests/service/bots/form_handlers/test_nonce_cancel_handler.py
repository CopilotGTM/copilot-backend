"""Tests for nonce-aware cancel handlers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestNonceCancelHandler:
    """Tests for NonceCancelFormHandler."""

    @pytest.mark.asyncio
    async def test_cancel_deal_consumes_nonce(self) -> None:
        from src.service.bots.form_handlers import FormHandlerRegistry

        handler = FormHandlerRegistry._handlers.get("create_deal_cancel")
        assert handler is not None

        with patch("src.service.bots.form_handlers.nonce.get_redis_client") as mock_redis:
            mock_client = MagicMock()
            mock_client.delete.return_value = 1
            mock_redis.return_value = mock_client

            result = await handler.handle(
                {"from": {"id": "user-1"}, "channelData": {}},
                {"nonce": "test-nonce", "organisation_id": "org-1"},
            )

            assert result.success is True
            assert "cancelled" in result.message.lower()

    @pytest.mark.asyncio
    async def test_cancel_note_consumes_nonce(self) -> None:
        from src.service.bots.form_handlers import FormHandlerRegistry

        handler = FormHandlerRegistry._handlers.get("add_deal_note_cancel")
        assert handler is not None

        with patch("src.service.bots.form_handlers.nonce.get_redis_client") as mock_redis:
            mock_client = MagicMock()
            mock_client.delete.return_value = 1
            mock_redis.return_value = mock_client

            result = await handler.handle(
                {"from": {"id": "user-1"}, "channelData": {}},
                {"nonce": "test-nonce", "organisation_id": "org-1"},
            )

            assert result.success is True
            assert "cancelled" in result.message.lower()
