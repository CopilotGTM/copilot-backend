"""Add Deal Note tool - sends a confirmation preview card to the user's chat channel.

The LLM is responsible for resolving the deal first (via search_deals),
then calling this tool with a confirmed deal_id and deal_name.

The card shows a read-only preview of the note with Confirm/Cancel buttons.
The actual note creation happens when the user clicks Confirm,
handled by AddDealNoteFormHandler in src/service/bots/form_handlers/.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access

This is an ORG-SCOPED tool. deal_id is NOT required at init.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.agents.tools.base import execute_with_capabilities
from src.service.bots.form_handlers.nonce import create_nonce
from src.service.bots.teams_bot_service import TeamsBotService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


def _escape_slack_mrkdwn(value: str) -> str:
    """Escape special characters for Slack mrkdwn."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_quote(value: str) -> str:
    """Render multi-line text as a Slack blockquote."""
    escaped = _escape_slack_mrkdwn(value)
    lines = escaped.splitlines() or [escaped]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


async def add_deal_note_form(
    *,
    context: AgentContext,
    deal_id: str,
    deal_name: str,
    note_content: str | None = None,
) -> dict[str, Any] | None:
    """Build and send a confirmation preview card for adding a deal note."""
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("add_deal_note_form: missing organisation_id in context")
        return None

    if not note_content or not note_content.strip():
        return {
            "status": "error",
            "message": (
                "Note content is required. Please ask the user what they'd like the note to say."
            ),
        }

    channel = (context.channel or "teams").lower()
    service_url = context.metadata.get("service_url", "")
    conversation_id = context.metadata.get("conversation_id", "")
    team_id = context.metadata.get("team_id", "")
    channel_id = context.metadata.get("channel_id", "")

    if channel == "slack":
        if not team_id or not channel_id:
            logger.warning("add_deal_note_form: missing Slack team_id/channel_id in metadata")
            return {
                "status": "error",
                "message": "Cannot send confirmation card: missing Slack channel context.",
            }
    elif not service_url or not conversation_id:
        logger.warning(
            "add_deal_note_form: missing Teams service_url/conversation_id in context metadata"
        )
        return {
            "status": "error",
            "message": "Cannot send confirmation card: missing Teams channel context.",
        }

    nonce = create_nonce("note_confirm")
    em = "\u2014"

    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": "Add Note to Deal",
            "size": "Medium",
            "weight": "Bolder",
            "wrap": True,
        },
        {
            "type": "FactSet",
            "facts": [
                {"title": "Deal", "value": deal_name},
            ],
        },
        {
            "type": "TextBlock",
            "text": f"**Note:** {note_content or em}",
            "wrap": True,
            "spacing": "Small",
        },
        {
            "type": "TextBlock",
            "text": "Please confirm or cancel.",
            "size": "Small",
            "isSubtle": True,
            "wrap": True,
            "spacing": "Medium",
        },
    ]

    confirm_data: dict[str, Any] = {
        "nonce": nonce,
        "organisation_id": str(organisation_id),
        "deal_id": deal_id,
        "deal_name": deal_name,
        "note_content": note_content or "",
    }

    cancel_data: dict[str, Any] = {
        "nonce": nonce,
        "organisation_id": str(organisation_id),
    }

    confirm_fallback = {"action": "add_deal_note_submit", **confirm_data}
    cancel_fallback = {"action": "add_deal_note_cancel", **cancel_data}

    actions = [
        {
            "type": "Action.Execute",
            "title": "Confirm",
            "style": "positive",
            "verb": "add_deal_note_submit",
            "data": confirm_data,
            "fallback": {
                "type": "Action.Submit",
                "title": "Confirm",
                "style": "positive",
                "data": confirm_fallback,
            },
        },
        {
            "type": "Action.Execute",
            "title": "Cancel",
            "style": "destructive",
            "verb": "add_deal_note_cancel",
            "data": cancel_data,
            "fallback": {
                "type": "Action.Submit",
                "title": "Cancel",
                "style": "destructive",
                "data": cancel_fallback,
            },
        },
    ]

    if channel == "slack":
        from src.service.bots.slack_bot_service import SlackBotService

        confirm_value = json.dumps(confirm_data, separators=(",", ":"))
        cancel_value = json.dumps(cancel_data, separators=(",", ":"))
        slack_blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "Add Note to Deal"},
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"*Deal:* {_escape_slack_mrkdwn(deal_name)}"},
                ],
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Note Preview*\n{_slack_quote(note_content or em)}",
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Click *Confirm* to add this note or *Cancel* to discard.",
                    }
                ],
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Confirm"},
                        "style": "primary",
                        "action_id": "add_deal_note_submit",
                        "value": confirm_value,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Cancel"},
                        "style": "danger",
                        "action_id": "add_deal_note_cancel",
                        "value": cancel_value,
                    },
                ],
            },
        ]

        bot_service = SlackBotService()
        sent = await bot_service.send_block_kit_message(
            team_id=team_id,
            channel_id=channel_id,
            blocks=slack_blocks,
            text_fallback=f"Add note preview for deal {deal_name}",
        )
    else:
        card_payload = {
            "text": " ",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": body,
                        "actions": actions,
                    },
                }
            ],
        }

        bot_service = TeamsBotService()
        sent = await bot_service.send_proactive_payload(
            service_url=service_url,
            conversation_id=conversation_id,
            payload=card_payload,
            text_fallback=f"Note preview for deal {deal_name}",
        )

    if sent:
        return {
            "status": "preview_sent",
            "message": (
                f"I've sent a preview to add a note to deal **{deal_name}**. "
                "Please review and click **Confirm**, or tell me what to change."
            ),
        }
    else:
        logger.error("add_deal_note_form: failed to send proactive payload")
        return {
            "status": "error",
            "message": "Failed to send the note preview. Please try again.",
        }


def create_add_deal_note(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the add_deal_note tool bound to a context.

    SECURITY: organisation_id comes from context (immutable, not a tool parameter).
    """

    async def add_deal_note_tool(
        deal_id: str,
        deal_name: str,
        note_content: str,
    ) -> dict[str, Any] | None:
        """Send a confirmation preview to add a note to an existing deal.

        IMPORTANT: Before calling this tool, you MUST first use search_deals to
        find the deal and confirm it with the user. Do NOT call this tool without
        a confirmed deal_id.

        note_content is REQUIRED. If the user says "add a note" without providing
        note text, ask them what they'd like the note to say before calling this tool.

        Workflow:
        1. User asks to add a note to a deal
        2. You call search_deals to find matching deals
        3. If multiple matches, ask user which deal they mean
        4. Once confirmed, call this tool with the deal_id, deal_name, and note_content

        After the preview is sent successfully, respond with a brief confirmation
        only. Do not add extra suggestions.

        Args:
            deal_id: UUID of the deal (from search_deals result)
            deal_name: Name of the deal (from search_deals result)
            note_content: The note text (REQUIRED)

        Returns:
            Status dict indicating whether the preview was sent successfully.
        """
        logger.info(
            "[TOOL CALLED] add_deal_note - deal_id='%s', deal_name='%s', has_content=%s",
            deal_id,
            deal_name,
            note_content is not None,
        )

        if not context.organisation_id:
            logger.warning("add_deal_note unavailable: missing organisation_id")
            return None

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="AddDealNote",
            tool_func=add_deal_note_form,
            parameters={
                "context": context,
                "deal_id": deal_id,
                "deal_name": deal_name,
                "note_content": note_content,
            },
            description="Adding deal note",
            fallback_value=None,
        )

        if result:
            logger.info(
                "[TOOL RESULT] add_deal_note status=%s",
                result.get("status"),
            )
        else:
            logger.info("[TOOL RESULT] add_deal_note returned None (error)")

        return dict(result) if result else None

    return add_deal_note_tool


__all__ = [
    "add_deal_note_form",
    "create_add_deal_note",
]
