"""Add Deal Note tool - sends an interactive note form to the user's chat channel.

This tool builds a FormDefinition with a note content field (optionally
pre-filled), renders it via the appropriate card builder, and sends it
as a proactive message to the user's chat.

The LLM is responsible for resolving the deal first (via search_deals),
then calling this tool with a confirmed deal_id and deal_name.

The actual note creation happens when the user submits the form,
handled by AddDealNoteFormHandler in src/service/bots/form_handlers/.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access

This is an ORG-SCOPED tool. deal_id is NOT required at init.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.adapters.card_builders import FormDefinition, FormField, get_card_builder
from src.agents.tools.base import execute_with_capabilities

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def add_deal_note_form(
    *,
    context: AgentContext,
    deal_id: str,
    deal_name: str,
    note_content: str | None = None,
) -> dict[str, Any] | None:
    """Send an interactive note form for a known deal to the user's chat.

    Args:
        context: AgentContext with organisation_id and channel metadata
        deal_id: UUID string of the deal (already resolved by LLM)
        deal_name: Display name of the deal (already resolved by LLM)
        note_content: Optional pre-filled note content

    Returns:
        Dict with status and message, or None on error
    """
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("add_deal_note_form: missing organisation_id in context")
        return None

    # Verify channel metadata is available for sending the card
    service_url = context.metadata.get("service_url", "")
    conversation_id = context.metadata.get("conversation_id", "")

    if not service_url or not conversation_id:
        logger.warning(
            "add_deal_note_form: missing service_url or conversation_id in context metadata"
        )
        return {
            "status": "error",
            "message": (
                "Cannot send interactive form: missing channel context. "
                "This tool is only available in Teams chat."
            ),
        }

    # Build the form definition
    fields: list[FormField] = [
        FormField(
            id="note_content",
            label="Note",
            field_type="text",
            required=True,
            value=note_content,
            placeholder="Type your note here...",
            multiline=True,
        ),
    ]

    form = FormDefinition(
        title="Add Note to Deal",
        subtitle=f"Deal: {deal_name}",
        fields=fields,
        submit_action="add_deal_note_submit",
        submit_label="Add Note",
        cancel_action="add_deal_note_cancel",
        cancel_label="Cancel",
        metadata={
            "organisation_id": str(organisation_id),
            "deal_id": deal_id,
            "deal_name": deal_name,
        },
    )

    # Build the card for the target channel
    try:
        builder = get_card_builder(context.channel)
        card_payload = builder.build(form)
    except ValueError:
        logger.warning("add_deal_note_form: no card builder for channel=%s", context.channel)
        return {
            "status": "error",
            "message": f"Interactive forms are not supported on {context.channel}.",
        }

    # Send the card via proactive messaging
    from src.service.bots.teams_bot_service import TeamsBotService

    bot_service = TeamsBotService()
    sent = await bot_service.send_proactive_payload(
        service_url=service_url,
        conversation_id=conversation_id,
        payload=card_payload,
        text_fallback="Add note to deal form",
    )

    if sent:
        return {
            "status": "form_sent",
            "message": (
                f"I've sent a form to add a note to deal **{deal_name}**. "
                "Please fill it out and submit."
            ),
        }
    else:
        logger.error("add_deal_note_form: failed to send proactive payload")
        return {
            "status": "error",
            "message": "Failed to send the note form. Please try again.",
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
        note_content: str | None = None,
    ) -> dict[str, Any] | None:
        """Send an interactive form to add a note to an existing deal.

        IMPORTANT: Before calling this tool, you MUST first use search_deals to
        find the deal and confirm it with the user. Do NOT call this tool without
        a confirmed deal_id.

        Workflow:
        1. User asks to add a note to a deal
        2. You call search_deals to find matching deals
        3. If multiple matches, ask user which deal they mean
        4. Once confirmed, call this tool with the deal_id and deal_name
        5. Optionally pre-fill note_content if the user provided note text

        After the form is sent successfully, respond with a brief confirmation
        only. Do not add extra suggestions.

        Args:
            deal_id: UUID of the deal (from search_deals result)
            deal_name: Name of the deal (from search_deals result)
            note_content: Optional note text to pre-fill in the form

        Returns:
            Status dict indicating whether the form was sent successfully.
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
            description="Sending deal note form",
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
