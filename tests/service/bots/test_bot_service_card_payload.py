"""Tests for card_payload handling in TeamsBotService.

Covers both Action.Execute (invoke) and Action.Submit (message) paths
when a form handler returns a FormHandlerResult with card_payload set.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from src.service.bots.form_handlers.base import FormHandlerResult
from src.service.bots.teams_bot_service import TeamsBotService


@pytest.fixture
def bot_service() -> TeamsBotService:
    with patch("src.service.bots.teams_bot_service.ExternalIdentityService"):
        return TeamsBotService()


# ---------------------------------------------------------------------------
# Action.Execute — _handle_adaptive_card_action
# ---------------------------------------------------------------------------


class TestAdaptiveCardActionCardPayload:
    """Card_payload should be returned directly as the invoke response."""

    @pytest.mark.asyncio
    @patch(
        "src.service.bots.form_handlers.FormHandlerRegistry.handle",
        new_callable=AsyncMock,
    )
    async def test_returns_card_payload_directly(
        self,
        mock_handle: AsyncMock,
        bot_service: TeamsBotService,
    ) -> None:
        test_card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": [{"type": "Input.Text", "id": "name"}],
        }
        mock_handle.return_value = FormHandlerResult(
            success=True, message="", card_payload=test_card
        )

        activity = {
            "value": {
                "action": {
                    "verb": "create_deal_edit",
                    "data": {"nonce": "abc", "name": "Deal"},
                }
            }
        }

        result = await bot_service._handle_adaptive_card_action(activity)

        assert result["statusCode"] == 200
        assert result["type"] == "application/vnd.microsoft.card.adaptive"
        assert result["value"] == test_card

    @pytest.mark.asyncio
    @patch(
        "src.service.bots.form_handlers.FormHandlerRegistry.handle",
        new_callable=AsyncMock,
    )
    async def test_success_without_card_payload_returns_confirmation(
        self,
        mock_handle: AsyncMock,
        bot_service: TeamsBotService,
    ) -> None:
        mock_handle.return_value = FormHandlerResult(success=True, message="Deal created!")

        activity = {
            "value": {
                "action": {
                    "verb": "create_deal_submit",
                    "data": {"nonce": "abc"},
                }
            }
        }

        result = await bot_service._handle_adaptive_card_action(activity)

        assert result["statusCode"] == 200
        assert result["type"] == "application/vnd.microsoft.card.adaptive"
        card = result["value"]
        assert card["type"] == "AdaptiveCard"
        # Confirmation card body contains a "Done" text block
        assert any("Done" in item.get("text", "") for item in card["body"])

    @pytest.mark.asyncio
    @patch(
        "src.service.bots.form_handlers.FormHandlerRegistry.handle",
        new_callable=AsyncMock,
    )
    async def test_failure_returns_error_card(
        self,
        mock_handle: AsyncMock,
        bot_service: TeamsBotService,
    ) -> None:
        mock_handle.return_value = FormHandlerResult(success=False, message="Nonce expired")

        activity = {
            "value": {
                "action": {
                    "verb": "create_deal_submit",
                    "data": {"nonce": "expired"},
                }
            }
        }

        result = await bot_service._handle_adaptive_card_action(activity)

        assert result["statusCode"] == 200
        card = result["value"]
        assert any("Error" in item.get("text", "") for item in card["body"])

    @pytest.mark.asyncio
    @patch(
        "src.service.bots.form_handlers.FormHandlerRegistry.handle",
        new_callable=AsyncMock,
    )
    async def test_no_handler_returns_error_card(
        self,
        mock_handle: AsyncMock,
        bot_service: TeamsBotService,
    ) -> None:
        mock_handle.return_value = None

        activity = {
            "value": {
                "action": {
                    "verb": "unknown_action",
                    "data": {},
                }
            }
        }

        result = await bot_service._handle_adaptive_card_action(activity)

        assert result["statusCode"] == 200
        card = result["value"]
        assert any("Error" in item.get("text", "") for item in card["body"])

    @pytest.mark.asyncio
    async def test_empty_verb_returns_message(
        self,
        bot_service: TeamsBotService,
    ) -> None:
        activity = {"value": {"action": {"verb": "", "data": {}}}}

        result = await bot_service._handle_adaptive_card_action(activity)

        assert result["statusCode"] == 200
        assert result["type"] == "application/vnd.microsoft.activity.message"


# ---------------------------------------------------------------------------
# Action.Submit fallback — _handle_form_submission
# ---------------------------------------------------------------------------


class TestFormSubmissionCardPayload:
    """Card_payload via Action.Submit should be sent as proactive Adaptive Card."""

    @pytest.mark.asyncio
    @patch(
        "src.service.bots.form_handlers.FormHandlerRegistry.handle",
        new_callable=AsyncMock,
    )
    async def test_card_payload_sends_proactive_card(
        self,
        mock_handle: AsyncMock,
        bot_service: TeamsBotService,
    ) -> None:
        test_card = {"type": "AdaptiveCard", "body": []}
        mock_handle.return_value = FormHandlerResult(
            success=True, message="", card_payload=test_card
        )
        bot_service.send_proactive_payload = AsyncMock(return_value=True)

        activity = {
            "type": "message",
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "conversation": {"id": "conv-123"},
            "from": {"id": "user-1"},
            "value": {"action": "create_deal_edit", "nonce": "abc"},
        }

        await bot_service._handle_form_submission(
            activity, activity["value"], activity["serviceUrl"]
        )

        bot_service.send_proactive_payload.assert_called_once()
        call_kwargs = bot_service.send_proactive_payload.call_args[1]
        assert call_kwargs["conversation_id"] == "conv-123"
        payload = call_kwargs["payload"]
        assert payload["attachments"][0]["content"] == test_card
        assert payload["attachments"][0]["contentType"] == (
            "application/vnd.microsoft.card.adaptive"
        )

    @pytest.mark.asyncio
    @patch(
        "src.service.bots.form_handlers.FormHandlerRegistry.handle",
        new_callable=AsyncMock,
    )
    async def test_plain_result_sends_text_reply(
        self,
        mock_handle: AsyncMock,
        bot_service: TeamsBotService,
    ) -> None:
        mock_handle.return_value = FormHandlerResult(success=True, message="Deal created!")
        bot_service._send_reply = AsyncMock()

        activity = {
            "type": "message",
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "conversation": {"id": "conv-123"},
            "from": {"id": "user-1"},
            "value": {"action": "create_deal_submit", "nonce": "abc"},
        }

        await bot_service._handle_form_submission(
            activity, activity["value"], activity["serviceUrl"]
        )

        bot_service._send_reply.assert_called_once()
        assert bot_service._send_reply.call_args[0][1] == "Deal created!"

    @pytest.mark.asyncio
    @patch(
        "src.service.bots.form_handlers.FormHandlerRegistry.handle",
        new_callable=AsyncMock,
    )
    async def test_no_handler_sends_fallback_reply(
        self,
        mock_handle: AsyncMock,
        bot_service: TeamsBotService,
    ) -> None:
        mock_handle.return_value = None
        bot_service._send_reply = AsyncMock()

        activity = {
            "type": "message",
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "conversation": {"id": "conv-123"},
            "from": {"id": "user-1"},
            "value": {"action": "unknown", "nonce": "abc"},
        }

        await bot_service._handle_form_submission(
            activity, activity["value"], activity["serviceUrl"]
        )

        bot_service._send_reply.assert_called_once()
        assert "couldn't process" in bot_service._send_reply.call_args[0][1].lower()
