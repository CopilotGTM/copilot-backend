"""Add Stakeholder tool - sends an interactive stakeholder form to the user's chat channel.

This tool builds a FormDefinition with stakeholder fields (optionally
pre-filled), renders it via the appropriate card builder, and sends it
as a proactive message to the user's chat.

The LLM is responsible for resolving the deal first (via search_deals),
then calling this tool with a confirmed deal_id and deal_name.

The actual stakeholder creation happens when the user submits the form,
handled by AddStakeholderFormHandler in src/service/bots/form_handlers/.

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
from src.service.crm.guards.internal_user_guard import is_internal_email

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

STANCE_CHOICES: list[dict[str, str]] = [
    {"title": "Unknown", "value": "UNKNOWN"},
    {"title": "Champion", "value": "CHAMPION"},
    {"title": "Supporter", "value": "SUPPORTER"},
    {"title": "Neutral", "value": "NEUTRAL"},
    {"title": "Detractor", "value": "DETRACTOR"},
    {"title": "Blocker", "value": "BLOCKER"},
]

DECISION_POWER_CHOICES: list[dict[str, str]] = [
    {"title": "Unknown", "value": "UNKNOWN"},
    {"title": "Decision Maker", "value": "DECISION_MAKER"},
    {"title": "Influencer", "value": "INFLUENCER"},
    {"title": "Evaluator", "value": "EVALUATOR"},
    {"title": "Gatekeeper", "value": "GATEKEEPER"},
    {"title": "End User", "value": "END_USER"},
]


async def add_stakeholder_form(
    *,
    context: AgentContext,
    deal_id: str,
    deal_name: str,
    email: str | None = None,
    name: str | None = None,
    job_title: str | None = None,
    phone_number: str | None = None,
    stance: str | None = None,
    decision_power: str | None = None,
) -> dict[str, Any] | None:
    """Send an interactive stakeholder form for a known deal to the user's chat.

    Args:
        context: AgentContext with organisation_id and channel metadata
        deal_id: UUID string of the deal (already resolved by LLM)
        deal_name: Display name of the deal (already resolved by LLM)
        email: Optional pre-filled email address
        name: Optional pre-filled stakeholder name
        job_title: Optional pre-filled job title
        stance: Optional pre-filled stance value (e.g. "CHAMPION")
        decision_power: Optional pre-filled decision power (e.g. "INFLUENCER")

    Returns:
        Dict with status and message, or None on error
    """
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("add_stakeholder_form: missing organisation_id in context")
        return None

    # Guardrail: reject internal team members before sending the form
    if email and await is_internal_email(context, email):
        logger.info(
            "add_stakeholder_form: blocked internal email=%s org=%s",
            email,
            organisation_id,
        )
        return {
            "status": "rejected_internal_user",
            "message": (
                "This email belongs to an internal team member and cannot be added "
                "as a stakeholder. Stakeholders must be external contacts only "
                "(clients, prospects, partners)."
            ),
        }

    channel = (context.channel or "teams").lower()
    service_url = context.metadata.get("service_url", "")
    conversation_id = context.metadata.get("conversation_id", "")
    team_id = context.metadata.get("team_id", "")
    channel_id = context.metadata.get("channel_id", "")

    if channel == "slack":
        if not team_id or not channel_id:
            logger.warning("add_stakeholder_form: missing Slack team_id/channel_id in metadata")
            return {
                "status": "error",
                "message": "Cannot send interactive form: missing Slack channel context.",
            }
    elif not service_url or not conversation_id:
        logger.warning(
            "add_stakeholder_form: missing Teams service_url/conversation_id in metadata"
        )
        return {
            "status": "error",
            "message": "Cannot send interactive form: missing Teams channel context.",
        }

    # Build the form definition
    fields: list[FormField] = [
        FormField(
            id="email",
            label="Email",
            field_type="text",
            required=True,
            value=email,
            placeholder="e.g. john@acme.com",
        ),
        FormField(
            id="name",
            label="Name",
            field_type="text",
            required=False,
            value=name,
            placeholder="e.g. John Smith",
        ),
        FormField(
            id="job_title",
            label="Job Title",
            field_type="text",
            required=False,
            value=job_title,
            placeholder="e.g. VP of Sales",
        ),
        FormField(
            id="phone_number",
            label="Phone Number",
            field_type="text",
            required=False,
            value=phone_number,
            placeholder="e.g. +1 (555) 123-4567",
        ),
        FormField(
            id="stance",
            label="Stance",
            field_type="choice",
            required=False,
            value=stance,
            choices=STANCE_CHOICES,
        ),
        FormField(
            id="decision_power",
            label="Decision Power",
            field_type="choice",
            required=False,
            value=decision_power,
            choices=DECISION_POWER_CHOICES,
        ),
    ]

    form = FormDefinition(
        title="Add Stakeholder",
        subtitle=f"Deal: {deal_name}",
        fields=fields,
        submit_action="add_stakeholder_submit",
        submit_label="Add Stakeholder",
        cancel_action="add_stakeholder_cancel",
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
        logger.warning("add_stakeholder_form: no card builder for channel=%s", context.channel)
        return {
            "status": "error",
            "message": f"Interactive forms are not supported on {context.channel}.",
        }

    # Send the card via the channel-specific bot service.
    if channel == "slack":
        from src.service.bots.slack_bot_service import SlackBotService

        bot_service = SlackBotService()
        blocks = card_payload.get("blocks")
        text_fallback = card_payload.get("text") or "Add stakeholder to deal form"
        if blocks:
            sent = await bot_service.send_block_kit_message(
                team_id=team_id,
                channel_id=channel_id,
                blocks=blocks,
                text_fallback=text_fallback,
            )
        else:
            sent = await bot_service._send_message(team_id, channel_id, text_fallback)
    else:
        from src.service.bots.teams_bot_service import TeamsBotService

        bot_service = TeamsBotService()
        sent = await bot_service.send_proactive_payload(
            service_url=service_url,
            conversation_id=conversation_id,
            payload=card_payload,
            text_fallback="Add stakeholder to deal form",
        )

    if sent:
        return {
            "status": "form_sent",
            "message": (
                f"I've sent a form to add a stakeholder to deal **{deal_name}**. "
                "Please fill it out and submit."
            ),
        }
    else:
        logger.error("add_stakeholder_form: failed to send proactive payload")
        return {
            "status": "error",
            "message": "Failed to send the stakeholder form. Please try again.",
        }


def create_add_stakeholder(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the add_stakeholder tool bound to a context.

    SECURITY: organisation_id comes from context (immutable, not a tool parameter).
    """

    async def add_stakeholder_tool(
        deal_id: str,
        deal_name: str,
        email: str | None = None,
        name: str | None = None,
        job_title: str | None = None,
        phone_number: str | None = None,
        stance: str | None = None,
        decision_power: str | None = None,
    ) -> dict[str, Any] | None:
        """Send an interactive form to add a stakeholder to an existing deal.

        IMPORTANT: Only add EXTERNAL contacts (clients, prospects, partners) as
        stakeholders. Do NOT add internal team members or anyone from your own
        organisation. Internal emails are rejected automatically.

        IMPORTANT: Before calling this tool, you MUST first use search_deals to
        find the deal and confirm it with the user. Do NOT call this tool without
        a confirmed deal_id.

        Workflow:
        1. User asks to add a stakeholder to a deal
        2. You call search_deals to find matching deals
        3. If multiple matches, ask user which deal they mean
        4. Once confirmed, call this tool with the deal_id and deal_name
        5. Pre-fill any stakeholder details the user mentioned (email, name, etc.)

        After the form is sent successfully, respond with a brief confirmation
        only. Do not add extra suggestions.

        Args:
            deal_id: UUID of the deal (from search_deals result)
            deal_name: Name of the deal (from search_deals result)
            email: Optional email address to pre-fill
            name: Optional stakeholder name to pre-fill
            job_title: Optional job title to pre-fill
            stance: Optional stance to pre-fill (UNKNOWN/CHAMPION/SUPPORTER/
                    NEUTRAL/DETRACTOR/BLOCKER)
            decision_power: Optional decision power to pre-fill (UNKNOWN/
                           DECISION_MAKER/INFLUENCER/EVALUATOR/GATEKEEPER/END_USER)

        Returns:
            Status dict indicating whether the form was sent successfully.
        """
        logger.info(
            "[TOOL CALLED] add_stakeholder - deal_id='%s', deal_name='%s', email=%s",
            deal_id,
            deal_name,
            email,
        )

        if not context.organisation_id:
            logger.warning("add_stakeholder unavailable: missing organisation_id")
            return None

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="AddStakeholder",
            tool_func=add_stakeholder_form,
            parameters={
                "context": context,
                "deal_id": deal_id,
                "deal_name": deal_name,
                "email": email,
                "name": name,
                "job_title": job_title,
                "phone_number": phone_number,
                "stance": stance,
                "decision_power": decision_power,
            },
            description="Adding stakeholder",
            fallback_value=None,
        )

        if result:
            logger.info(
                "[TOOL RESULT] add_stakeholder status=%s",
                result.get("status"),
            )
        else:
            logger.info("[TOOL RESULT] add_stakeholder returned None (error)")

        return dict(result) if result else None

    return add_stakeholder_tool


__all__ = [
    "add_stakeholder_form",
    "create_add_stakeholder",
]
