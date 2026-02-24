"""Tests for add_deal_note confirmation card (preview mode)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from src.agents.tools.crm.add_deal_note import add_deal_note_form


@pytest.fixture
def mock_context() -> MagicMock:
    ctx = MagicMock()
    ctx.organisation_id = UUID("11111111-1111-1111-1111-111111111111")
    ctx.channel = "teams"
    ctx.metadata = {
        "service_url": "https://smba.trafficmanager.net/teams/",
        "conversation_id": "conv-123",
    }
    return ctx


class TestAddDealNoteConfirmation:
    """Tests for confirmation card behavior."""

    @pytest.mark.asyncio
    @patch("src.agents.tools.crm.add_deal_note.TeamsBotService")
    @patch("src.agents.tools.crm.add_deal_note.create_nonce", return_value="note-nonce-123")
    async def test_sends_preview_card(
        self,
        mock_nonce: MagicMock,
        mock_bot_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_bot = MagicMock()
        mock_bot.send_proactive_payload = AsyncMock(return_value=True)
        mock_bot_cls.return_value = mock_bot

        result = await add_deal_note_form(
            context=mock_context,
            deal_id="22222222-2222-2222-2222-222222222222",
            deal_name="Acme Corp",
            note_content="Follow up on pricing discussion",
        )

        assert result is not None
        assert result["status"] == "preview_sent"

        payload = mock_bot.send_proactive_payload.call_args[1]["payload"]
        card_content = payload["attachments"][0]["content"]

        # Should have FactSet, not Input fields
        body_types = [item["type"] for item in card_content["body"]]
        assert "FactSet" in body_types

        # Confirm button carries note data + nonce
        confirm_action = card_content["actions"][0]
        assert confirm_action["data"]["deal_id"] == "22222222-2222-2222-2222-222222222222"
        assert confirm_action["data"]["note_content"] == "Follow up on pricing discussion"
        assert confirm_action["data"]["nonce"] == "note-nonce-123"

    @pytest.mark.asyncio
    @patch("src.agents.tools.crm.add_deal_note.TeamsBotService")
    @patch("src.agents.tools.crm.add_deal_note.create_nonce", return_value="nonce-456")
    async def test_cancel_button_has_nonce(
        self,
        mock_nonce: MagicMock,
        mock_bot_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_bot = MagicMock()
        mock_bot.send_proactive_payload = AsyncMock(return_value=True)
        mock_bot_cls.return_value = mock_bot

        await add_deal_note_form(
            context=mock_context,
            deal_id="22222222-2222-2222-2222-222222222222",
            deal_name="Acme Corp",
            note_content="Test note",
        )

        payload = mock_bot.send_proactive_payload.call_args[1]["payload"]
        cancel_action = payload["attachments"][0]["content"]["actions"][1]
        assert cancel_action["verb"] == "add_deal_note_cancel"
        assert cancel_action["data"]["nonce"] == "nonce-456"

    @pytest.mark.asyncio
    async def test_rejects_empty_note_content(self, mock_context: MagicMock) -> None:
        result = await add_deal_note_form(
            context=mock_context,
            deal_id="22222222-2222-2222-2222-222222222222",
            deal_name="Acme Corp",
            note_content="",
        )

        assert result is not None
        assert result["status"] == "error"
        assert "required" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_rejects_none_note_content(self, mock_context: MagicMock) -> None:
        result = await add_deal_note_form(
            context=mock_context,
            deal_id="22222222-2222-2222-2222-222222222222",
            deal_name="Acme Corp",
            note_content=None,
        )

        assert result is not None
        assert result["status"] == "error"
        assert "required" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_rejects_whitespace_note_content(self, mock_context: MagicMock) -> None:
        result = await add_deal_note_form(
            context=mock_context,
            deal_id="22222222-2222-2222-2222-222222222222",
            deal_name="Acme Corp",
            note_content="   ",
        )

        assert result is not None
        assert result["status"] == "error"
        assert "required" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_missing_org_returns_none(self, mock_context: MagicMock) -> None:
        mock_context.organisation_id = None

        result = await add_deal_note_form(
            context=mock_context,
            deal_id="22222222-2222-2222-2222-222222222222",
            deal_name="Acme Corp",
            note_content="Some note",
        )

        assert result is None

    @pytest.mark.asyncio
    @patch("src.agents.tools.crm.add_deal_note.TeamsBotService")
    @patch("src.agents.tools.crm.add_deal_note.create_nonce", return_value="nonce-fail")
    async def test_send_failure_returns_error(
        self,
        mock_nonce: MagicMock,
        mock_bot_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_bot = MagicMock()
        mock_bot.send_proactive_payload = AsyncMock(return_value=False)
        mock_bot_cls.return_value = mock_bot

        result = await add_deal_note_form(
            context=mock_context,
            deal_id="22222222-2222-2222-2222-222222222222",
            deal_name="Acme Corp",
            note_content="Some note",
        )

        assert result is not None
        assert result["status"] == "error"
        assert "failed" in result["message"].lower()
