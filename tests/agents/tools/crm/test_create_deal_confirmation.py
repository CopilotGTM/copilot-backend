"""Tests for create_deal confirmation card (preview mode)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from src.agents.tools.crm.create_deal import create_deal_form


@pytest.fixture
def mock_context() -> MagicMock:
    ctx = MagicMock()
    ctx.organisation_id = UUID("11111111-1111-1111-1111-111111111111")
    ctx.channel = "teams"
    ctx.metadata = {
        "service_url": "https://smba.trafficmanager.net/teams/",
        "conversation_id": "conv-123",
    }

    # Mock acquire_session as async context manager
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))

    async_cm = AsyncMock()
    async_cm.__aenter__ = AsyncMock(return_value=mock_session)
    async_cm.__aexit__ = AsyncMock(return_value=False)
    ctx.acquire_session = MagicMock(return_value=async_cm)

    return ctx


class TestCreateDealConfirmation:
    """Tests for confirmation card behavior."""

    @pytest.mark.asyncio
    @patch("src.agents.tools.crm.create_deal.TeamsBotService")
    @patch("src.agents.tools.crm.create_deal.create_nonce", return_value="test-nonce-123")
    async def test_sends_preview_card_not_form(
        self,
        mock_nonce: MagicMock,
        mock_bot_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_bot = MagicMock()
        mock_bot.send_proactive_payload = AsyncMock(return_value=True)
        mock_bot_cls.return_value = mock_bot

        result = await create_deal_form(
            context=mock_context,
            name="Acme Corp Deal",
            amount=50000,
            currency="USD",
        )

        assert result is not None
        assert result["status"] == "preview_sent"

        # Verify the card was sent
        mock_bot.send_proactive_payload.assert_called_once()
        payload = mock_bot.send_proactive_payload.call_args[1]["payload"]
        card_content = payload["attachments"][0]["content"]

        # Should have FactSet (read-only), not Input fields
        body_types = [item["type"] for item in card_content["body"]]
        assert "FactSet" in body_types
        assert not any("Input" in t for t in body_types)

        # Confirm button should carry deal data + nonce
        confirm_action = card_content["actions"][0]
        assert confirm_action["verb"] == "create_deal_submit"
        assert confirm_action["data"]["name"] == "Acme Corp Deal"
        assert confirm_action["data"]["amount"] == 50000
        assert confirm_action["data"]["nonce"] == "test-nonce-123"

    @pytest.mark.asyncio
    @patch("src.agents.tools.crm.create_deal.TeamsBotService")
    @patch("src.agents.tools.crm.create_deal.create_nonce", return_value="test-nonce-456")
    async def test_empty_fields_shown_as_dash(
        self,
        mock_nonce: MagicMock,
        mock_bot_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_bot = MagicMock()
        mock_bot.send_proactive_payload = AsyncMock(return_value=True)
        mock_bot_cls.return_value = mock_bot

        result = await create_deal_form(
            context=mock_context,
            name="Minimal Deal",
        )

        assert result is not None
        payload = mock_bot.send_proactive_payload.call_args[1]["payload"]
        card_content = payload["attachments"][0]["content"]

        # Find the FactSet
        fact_set = next(item for item in card_content["body"] if item["type"] == "FactSet")
        facts_dict = {f["title"]: f["value"] for f in fact_set["facts"]}

        assert facts_dict["Name"] == "Minimal Deal"
        assert facts_dict["Amount"] == "\u2014"  # em dash for empty

    @pytest.mark.asyncio
    @patch("src.agents.tools.crm.create_deal.TeamsBotService")
    @patch("src.agents.tools.crm.create_deal.create_nonce", return_value="nonce-789")
    async def test_cancel_button_has_nonce(
        self,
        mock_nonce: MagicMock,
        mock_bot_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_bot = MagicMock()
        mock_bot.send_proactive_payload = AsyncMock(return_value=True)
        mock_bot_cls.return_value = mock_bot

        await create_deal_form(context=mock_context, name="Test")

        payload = mock_bot.send_proactive_payload.call_args[1]["payload"]
        card_content = payload["attachments"][0]["content"]

        # Cancel is now the third button (index 2) after Confirm and Edit
        cancel_action = card_content["actions"][2]
        assert cancel_action["verb"] == "create_deal_cancel"
        assert cancel_action["data"]["nonce"] == "nonce-789"

    @pytest.mark.asyncio
    @patch("src.agents.tools.crm.create_deal.TeamsBotService")
    @patch("src.agents.tools.crm.create_deal.create_nonce", return_value="nonce-edit")
    async def test_edit_button_present_with_deal_data(
        self,
        mock_nonce: MagicMock,
        mock_bot_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_bot = MagicMock()
        mock_bot.send_proactive_payload = AsyncMock(return_value=True)
        mock_bot_cls.return_value = mock_bot

        await create_deal_form(
            context=mock_context,
            name="Acme Deal",
            amount=25000,
            currency="EUR",
        )

        payload = mock_bot.send_proactive_payload.call_args[1]["payload"]
        card_content = payload["attachments"][0]["content"]

        # Three buttons: Confirm, Edit, Cancel
        assert len(card_content["actions"]) == 3

        edit_action = card_content["actions"][1]
        assert edit_action["verb"] == "create_deal_edit"
        assert edit_action["title"] == "Edit"
        assert edit_action["data"]["name"] == "Acme Deal"
        assert edit_action["data"]["amount"] == 25000
        assert edit_action["data"]["nonce"] == "nonce-edit"

    @pytest.mark.asyncio
    async def test_rejects_empty_name(self, mock_context: MagicMock) -> None:
        result = await create_deal_form(context=mock_context, name="")

        assert result is not None
        assert result["status"] == "error"
        assert "name is required" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_rejects_none_name(self, mock_context: MagicMock) -> None:
        result = await create_deal_form(context=mock_context, name=None)

        assert result is not None
        assert result["status"] == "error"
        assert "name is required" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_rejects_whitespace_name(self, mock_context: MagicMock) -> None:
        result = await create_deal_form(context=mock_context, name="   ")

        assert result is not None
        assert result["status"] == "error"
        assert "name is required" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_missing_org_returns_none(self, mock_context: MagicMock) -> None:
        mock_context.organisation_id = None

        result = await create_deal_form(context=mock_context, name="Test Deal")

        assert result is None

    @pytest.mark.asyncio
    async def test_missing_service_url_returns_error(self, mock_context: MagicMock) -> None:
        mock_context.metadata = {"conversation_id": "conv-123"}

        result = await create_deal_form(context=mock_context, name="Test Deal")

        assert result is not None
        assert result["status"] == "error"
        assert (
            "channel context" in result["message"].lower() or "missing" in result["message"].lower()
        )

    @pytest.mark.asyncio
    async def test_missing_conversation_id_returns_error(self, mock_context: MagicMock) -> None:
        mock_context.metadata = {"service_url": "https://smba.trafficmanager.net/teams/"}

        result = await create_deal_form(context=mock_context, name="Test Deal")

        assert result is not None
        assert result["status"] == "error"

    @pytest.mark.asyncio
    @patch("src.agents.tools.crm.create_deal.TeamsBotService")
    @patch("src.agents.tools.crm.create_deal.create_nonce", return_value="test-nonce")
    async def test_send_failure_returns_error(
        self,
        mock_nonce: MagicMock,
        mock_bot_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_bot = MagicMock()
        mock_bot.send_proactive_payload = AsyncMock(return_value=False)
        mock_bot_cls.return_value = mock_bot

        result = await create_deal_form(context=mock_context, name="Test Deal")

        assert result is not None
        assert result["status"] == "error"
        assert "failed" in result["message"].lower()

    @pytest.mark.asyncio
    @patch("src.agents.tools.crm.create_deal.TeamsBotService")
    @patch("src.agents.tools.crm.create_deal.create_nonce", return_value="nonce-all")
    async def test_all_fields_populated_in_factset(
        self,
        mock_nonce: MagicMock,
        mock_bot_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_bot = MagicMock()
        mock_bot.send_proactive_payload = AsyncMock(return_value=True)
        mock_bot_cls.return_value = mock_bot

        result = await create_deal_form(
            context=mock_context,
            name="Full Deal",
            amount=99000,
            currency="EUR",
            description="Desc text",
            close_date="2026-12-31",
            priority="high",
            account_name="FullCorp",
            account_domain="fullcorp.com",
        )

        assert result is not None
        assert result["status"] == "preview_sent"

        payload = mock_bot.send_proactive_payload.call_args[1]["payload"]
        card_content = payload["attachments"][0]["content"]

        fact_set = next(item for item in card_content["body"] if item["type"] == "FactSet")
        facts_dict = {f["title"]: f["value"] for f in fact_set["facts"]}

        assert facts_dict["Name"] == "Full Deal"
        assert "99,000" in facts_dict["Amount"]
        assert "EUR" in facts_dict["Amount"]
        assert facts_dict["Close Date"] == "2026-12-31"
        assert facts_dict["Priority"] == "High"
        assert facts_dict["Account"] == "FullCorp"
        assert facts_dict["Description"] == "Desc text"

    @pytest.mark.asyncio
    @patch("src.agents.tools.crm.create_deal.TeamsBotService")
    @patch("src.agents.tools.crm.create_deal.create_nonce", return_value="nonce-fb")
    async def test_edit_button_has_action_submit_fallback(
        self,
        mock_nonce: MagicMock,
        mock_bot_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_bot = MagicMock()
        mock_bot.send_proactive_payload = AsyncMock(return_value=True)
        mock_bot_cls.return_value = mock_bot

        await create_deal_form(context=mock_context, name="Fallback Test")

        payload = mock_bot.send_proactive_payload.call_args[1]["payload"]
        card_content = payload["attachments"][0]["content"]

        edit_action = card_content["actions"][1]
        assert "fallback" in edit_action
        fallback = edit_action["fallback"]
        assert fallback["type"] == "Action.Submit"
        assert fallback["data"]["action"] == "create_deal_edit"
        assert fallback["data"]["name"] == "Fallback Test"

    @pytest.mark.asyncio
    @patch("src.service.bots.slack_bot_service.SlackBotService")
    @patch("src.agents.tools.crm.create_deal.create_nonce", return_value="nonce-slack")
    async def test_slack_preview_sends_confirm_cancel_buttons(
        self,
        mock_nonce: MagicMock,
        mock_slack_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_context.channel = "slack"
        mock_context.metadata = {"team_id": "T123", "channel_id": "C123"}

        mock_slack = MagicMock()
        mock_slack.send_block_kit_message = AsyncMock(return_value=True)
        mock_slack_cls.return_value = mock_slack

        result = await create_deal_form(
            context=mock_context,
            name="Slack Deal",
            amount=12000,
            currency="USD",
        )

        assert result is not None
        assert result["status"] == "preview_sent"
        mock_slack.send_block_kit_message.assert_called_once()

        blocks = mock_slack.send_block_kit_message.call_args[1]["blocks"]
        assert blocks[0]["type"] == "header"
        assert blocks[0]["text"]["text"] == "Create New Deal"
        field_section = next(b for b in blocks if b["type"] == "section" and "fields" in b)
        field_labels = [f["text"].split("\n")[0] for f in field_section["fields"]]
        assert "*Name*" in field_labels
        assert "*Amount*" in field_labels
        assert any(
            "*Description*" in b["text"]["text"]
            for b in blocks
            if b["type"] == "section" and "text" in b
        )
        actions_block = next(b for b in blocks if b["type"] == "actions")
        action_ids = [el["action_id"] for el in actions_block["elements"]]
        assert action_ids == ["create_deal_submit", "create_deal_cancel"]
