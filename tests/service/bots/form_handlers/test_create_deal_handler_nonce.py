"""Tests for nonce validation in CreateDealFormHandler."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from src.service.bots.form_handlers.create_deal_handler import CreateDealFormHandler


@pytest.fixture
def handler() -> CreateDealFormHandler:
    return CreateDealFormHandler()


@pytest.fixture
def activity() -> dict:
    return {
        "from": {"id": "teams-user-123"},
        "channelData": {"tenant": {"id": "tenant-456"}},
    }


class TestCreateDealHandlerNonce:
    """Tests for nonce validation."""

    @pytest.mark.asyncio
    @patch("src.service.bots.form_handlers.create_deal_handler.validate_and_consume_nonce")
    async def test_rejects_expired_nonce(
        self,
        mock_validate: MagicMock,
        handler: CreateDealFormHandler,
        activity: dict,
    ) -> None:
        mock_validate.return_value = False

        result = await handler.handle(
            activity,
            {
                "nonce": "expired-nonce",
                "name": "Acme Deal",
                "organisation_id": "11111111-1111-1111-1111-111111111111",
            },
        )

        assert result.success is False
        assert "expired" in result.message.lower()
        mock_validate.assert_called_once_with("deal_confirm", "expired-nonce")

    @pytest.mark.asyncio
    @patch("src.service.bots.form_handlers.create_deal_handler.validate_and_consume_nonce")
    async def test_rejects_missing_nonce(
        self,
        mock_validate: MagicMock,
        handler: CreateDealFormHandler,
        activity: dict,
    ) -> None:
        result = await handler.handle(
            activity,
            {
                "name": "Acme Deal",
                "organisation_id": "11111111-1111-1111-1111-111111111111",
            },
        )

        assert result.success is False
        assert "expired" in result.message.lower() or "invalid" in result.message.lower()
