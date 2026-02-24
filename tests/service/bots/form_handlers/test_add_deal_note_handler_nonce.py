"""Tests for nonce validation in AddDealNoteFormHandler."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from src.service.bots.form_handlers.add_deal_note_handler import AddDealNoteFormHandler


@pytest.fixture
def handler() -> AddDealNoteFormHandler:
    return AddDealNoteFormHandler()


@pytest.fixture
def activity() -> dict:
    return {
        "from": {"id": "teams-user-123"},
        "channelData": {"tenant": {"id": "tenant-456"}},
    }


class TestAddDealNoteHandlerNonce:
    """Tests for nonce validation."""

    @pytest.mark.asyncio
    @patch("src.service.bots.form_handlers.add_deal_note_handler.validate_and_consume_nonce")
    async def test_rejects_expired_nonce(
        self,
        mock_validate: MagicMock,
        handler: AddDealNoteFormHandler,
        activity: dict,
    ) -> None:
        mock_validate.return_value = False

        result = await handler.handle(
            activity,
            {
                "nonce": "expired-nonce",
                "note_content": "Some note",
                "deal_id": "22222222-2222-2222-2222-222222222222",
                "organisation_id": "11111111-1111-1111-1111-111111111111",
            },
        )

        assert result.success is False
        assert "expired" in result.message.lower()
        mock_validate.assert_called_once_with("note_confirm", "expired-nonce")

    @pytest.mark.asyncio
    @patch("src.service.bots.form_handlers.add_deal_note_handler.validate_and_consume_nonce")
    async def test_rejects_missing_nonce(
        self,
        mock_validate: MagicMock,
        handler: AddDealNoteFormHandler,
        activity: dict,
    ) -> None:
        result = await handler.handle(
            activity,
            {
                "note_content": "Some note",
                "deal_id": "22222222-2222-2222-2222-222222222222",
            },
        )

        assert result.success is False
