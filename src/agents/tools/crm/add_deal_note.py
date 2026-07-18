"""Add Deal Note agent tool and legacy confirmation-card fallback.

The normal agent path creates the note directly once the deal and note content
are known. The preview-card helper remains for previously sent Teams and Slack
cards, whose submissions are still handled by AddDealNoteFormHandler in
src/service/bots/form_handlers/.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access

This is an ORG-SCOPED tool. deal_id is NOT required at init.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from src.agents.tools.base import execute_with_capabilities
from src.agents.tools.crm.access import AgentCRMAccessError, resolve_agent_access_actor
from src.service.bots.form_handlers.nonce import create_nonce
from src.service.bots.teams_bot_service import TeamsBotService
from src.service.crm.notes.contracts import NotesServiceError, NoteWriteContext
from src.service.crm.notes.insight_dispatch_service import NotesInsightDispatchService
from src.service.crm.notes.write_service import NotesWriteService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

_BOT_CHANNEL_PAYLOAD_KEY = "_bot_channel_payload"


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _public_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if not key.startswith("_")}


def _escape_slack_mrkdwn(value: str) -> str:
    """Escape special characters for Slack mrkdwn."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_quote(value: str) -> str:
    """Render multi-line text as a Slack blockquote."""
    escaped = _escape_slack_mrkdwn(value)
    lines = escaped.splitlines() or [escaped]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def _note_created_text_fallback(deal_name: str) -> str:
    return f"Added note to {deal_name}."


def _note_result_timestamp(note: Any) -> str | None:
    timestamp = getattr(note, "source_created_at", None) or getattr(note, "created_at", None)
    if timestamp is None:
        return None
    if hasattr(timestamp, "isoformat"):
        return cast(str, timestamp.isoformat())
    return str(timestamp)


def _build_teams_success_payload(
    *,
    deal_name: str,
    title: str | None,
    body: str,
) -> dict[str, Any]:
    facts = [{"title": "Deal", "value": deal_name}]
    if title:
        facts.append({"title": "Title", "value": title})
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": "Note added",
                "size": "Medium",
                "weight": "Bolder",
                "wrap": True,
                "color": "Good",
            },
            {"type": "FactSet", "facts": facts},
            {
                "type": "TextBlock",
                "text": body,
                "wrap": True,
                "spacing": "Small",
            },
        ],
    }
    return {
        "type": "message",
        "content": {
            "text": " ",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": card,
                }
            ],
        },
        "text_fallback": _note_created_text_fallback(deal_name),
        "attachments": None,
        "source_text": None,
    }


def _build_slack_success_payload(
    *,
    deal_name: str,
    title: str | None,
    body: str,
) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Note added"},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"*Deal:* {_escape_slack_mrkdwn(deal_name)}"},
            ],
        },
    ]
    if title:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{_escape_slack_mrkdwn(title)}*"},
            }
        )
    blocks.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": _slack_quote(body)},
        }
    )
    return {
        "type": "message",
        "content": {
            "text": _note_created_text_fallback(deal_name),
            "blocks": blocks,
        },
        "text_fallback": _note_created_text_fallback(deal_name),
        "attachments": None,
        "source_text": None,
    }


def _build_note_added_channel_payload(
    *,
    context: AgentContext,
    deal_name: str,
    title: str | None,
    body: str,
) -> dict[str, Any] | None:
    channel = (context.channel or "").lower()
    if channel == "teams":
        return _build_teams_success_payload(
            deal_name=deal_name,
            title=title,
            body=body,
        )
    if channel == "slack":
        return _build_slack_success_payload(
            deal_name=deal_name,
            title=title,
            body=body,
        )
    return None


def _note_result(
    *,
    context: AgentContext,
    deal_id: UUID,
    deal_name: str,
    note: Any,
    created_new: bool,
    insight_job_id: UUID | None = None,
) -> dict[str, Any]:
    status = "created" if created_new else "exists"
    body = note.body or ""
    title = getattr(note, "title", None) or getattr(note, "subject", None)
    display_title = title or (" ".join(body.split())[:30].rstrip() if body else None)
    result: dict[str, Any] = {
        "status": status,
        "note_id": str(note.activity_id),
        "deal_id": str(deal_id),
        "deal_name": deal_name,
        "title": title,
        "body": body,
        "display_title": display_title,
        "created_at": _note_result_timestamp(note),
        "message": (
            f"Added note to **{deal_name}**."
            if created_new
            else f"Identical note already exists for **{deal_name}**."
        ),
    }
    if insight_job_id:
        result["insight_job_id"] = str(insight_job_id)

    channel_payload = _build_note_added_channel_payload(
        context=context,
        deal_name=deal_name,
        title=title,
        body=body,
    )
    if channel_payload:
        result[_BOT_CHANNEL_PAYLOAD_KEY] = channel_payload
    return result


async def add_deal_note_direct(
    *,
    context: AgentContext,
    deal_id: str,
    deal_name: str,
    body: str | None = None,
    title: str | None = None,
    note_content: str | None = None,
    subject: str | None = None,
) -> dict[str, Any]:
    """Create a user note directly through the deal notes service."""
    organisation_id = context.organisation_id
    user_id = context.user_id
    clean_deal_name = _clean_optional(deal_name)
    clean_body = _clean_optional(body) or _clean_optional(note_content)
    clean_title = _clean_optional(title) or _clean_optional(subject)

    if not organisation_id:
        logger.warning("add_deal_note_direct: missing organisation_id in context")
        return {"status": "error", "message": "Cannot add note: missing organisation context."}
    if not user_id:
        logger.warning("add_deal_note_direct: missing user_id in context")
        return {"status": "error", "message": "Could not identify your account. Please sign in."}
    if not clean_deal_name:
        return {"status": "error", "message": "Deal name is required before adding a note."}
    if not clean_body:
        return {
            "status": "error",
            "message": "Note body is required. Please ask the user what the note should say.",
        }
    if len(clean_body) > 10000:
        return {"status": "error", "message": "Note body must be 10,000 characters or less."}
    if clean_title and len(clean_title) > 500:
        return {"status": "error", "message": "Note title must be 500 characters or less."}

    try:
        deal_uuid = UUID(deal_id)
    except (TypeError, ValueError):
        return {
            "status": "error",
            "message": "Invalid deal information. Please search the deal again.",
        }

    user_name = _clean_optional(context.metadata.get("user_name")) or _clean_optional(
        context.metadata.get("user_full_name")
    )

    async with context.acquire_session() as session:
        try:
            access_actor = await resolve_agent_access_actor(context)
        except AgentCRMAccessError as exc:
            return {"status": "error", "message": str(exc)}
        notes_write = NotesWriteService(session, organisation_id, access_actor)
        insight_dispatch = NotesInsightDispatchService(session, organisation_id)
        try:
            create_result = await notes_write.create_user_note(
                deal_id=deal_uuid,
                content=clean_body,
                subject=clean_title,
                context=NoteWriteContext(
                    source="agent",
                    actor_user_id=user_id,
                    actor_name=user_name,
                    actor_email=context.user_email,
                    created_via="agent_add_deal_note",
                ),
            )
            await session.commit()

            insight_job_id = None
            if create_result.created_new:
                try:
                    insight_job_id = await insight_dispatch.trigger_deal_insight_pipeline(
                        deal_id=deal_uuid,
                        user_id=user_id,
                        trigger_source="note_creation",
                        trigger_source_id=create_result.note.activity_id,
                    )
                except Exception:
                    logger.exception(
                        "add_deal_note_direct: note created but insight dispatch failed"
                    )

            return _note_result(
                context=context,
                deal_id=deal_uuid,
                deal_name=clean_deal_name,
                note=create_result.note,
                created_new=create_result.created_new,
                insight_job_id=insight_job_id,
            )
        except NotesServiceError as exc:
            await session.rollback()
            logger.warning("add_deal_note_direct: DealNotesServiceError: %s", exc.message)
            return {"status": "error", "message": f"Could not add note: {exc.message}"}
        except Exception:
            await session.rollback()
            logger.exception("add_deal_note_direct: unexpected error")
            return {
                "status": "error",
                "message": "An unexpected error occurred while adding the note.",
            }


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
        slack_blocks: list[dict[str, Any]] = [
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

        slack_service = SlackBotService()
        sent = await slack_service.send_block_kit_message(
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

        teams_service = TeamsBotService()
        sent = await teams_service.send_proactive_payload(
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
        body: str,
        title: str | None = None,
    ) -> dict[str, Any] | None:
        """Create a note directly on an existing deal.

        IMPORTANT: Before calling this tool, you MUST first use search_deals to
        find the deal. Do NOT call this tool without a resolved deal_id.

        body is REQUIRED. If the user says "add a note" without providing
        note text, ask them what they'd like the note to say before calling this tool.

        Workflow:
        1. User asks to add a note to a deal
        2. You call search_deals to find matching deals
        3. If multiple matches, ask user which deal they mean
        4. Once confirmed, call this tool with the deal_id, deal_name, and body

        Args:
            deal_id: UUID of the deal (from search_deals result)
            deal_name: Name of the deal (from search_deals result)
            body: The note text (REQUIRED)
            title: Optional note title

        Returns:
            Structured status dict with created note details or an error message.
        """
        logger.info(
            "[TOOL CALLED] add_deal_note - deal_id='%s', deal_name='%s', has_content=%s",
            deal_id,
            deal_name,
            body is not None,
        )

        if not context.organisation_id:
            logger.warning("add_deal_note unavailable: missing organisation_id")
            return None

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="AddDealNote",
            tool_func=add_deal_note_direct,
            parameters={
                "context": context,
                "deal_id": deal_id,
                "deal_name": deal_name,
                "body": body,
                "title": title,
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

        return _public_tool_result(dict(result)) if result else None

    return add_deal_note_tool


__all__ = [
    "add_deal_note_direct",
    "add_deal_note_form",
    "create_add_deal_note",
]
