"""Create Deal tool - sends a confirmation preview card for deal creation.

This tool builds a read-only Adaptive Card with deal details extracted by
the agent, and sends it to the user's chat. The user clicks Confirm to
create the deal, Edit to switch to an editable form, or Cancel to discard.

The actual deal creation happens when the user clicks Confirm,
handled by CreateDealFormHandler in src/service/bots/form_handlers/.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access

This is an ORG-SCOPED tool. deal_id is NOT required.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealStage,
    CRMPipeline,
)
from src.service.bots.form_handlers.nonce import create_nonce
from src.service.bots.teams_bot_service import TeamsBotService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


def _escape_slack_mrkdwn(value: str) -> str:
    """Escape special characters for Slack mrkdwn."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_field(label: str, value: Any) -> dict[str, str]:
    """Build a Slack section field with label + value."""
    text = _escape_slack_mrkdwn(str(value))
    return {"type": "mrkdwn", "text": f"*{label}*\n{text}"}


def _slack_quote(value: str) -> str:
    """Render multi-line text as a Slack blockquote."""
    escaped = _escape_slack_mrkdwn(value)
    lines = escaped.splitlines() or [escaped]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


async def create_deal_form(
    *,
    context: AgentContext,
    name: str | None = None,
    amount: float | None = None,
    currency: str | None = None,
    description: str | None = None,
    close_date: str | None = None,
    priority: str | None = None,
    account_name: str | None = None,
    account_domain: str | None = None,
) -> dict[str, Any] | None:
    """Build and send a confirmation preview card for deal creation.

    Queries pipelines/stages for defaults, builds a read-only Adaptive Card,
    and sends it to the user's chat. The user clicks Confirm to create the deal.
    """
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("create_deal_form: missing organisation_id in context")
        return None

    if not name or not name.strip():
        return {
            "status": "error",
            "message": (
                "Deal name is required. Please ask the user for a deal name "
                "before calling this tool."
            ),
        }

    channel = (context.channel or "teams").lower()
    service_url = context.metadata.get("service_url", "")
    conversation_id = context.metadata.get("conversation_id", "")
    team_id = context.metadata.get("team_id", "")
    channel_id = context.metadata.get("channel_id", "")

    if channel == "slack":
        if not team_id or not channel_id:
            logger.warning("create_deal_form: missing Slack team_id/channel_id in metadata")
            return {
                "status": "error",
                "message": "Cannot send confirmation card: missing Slack channel context.",
            }
    elif not service_url or not conversation_id:
        logger.warning(
            "create_deal_form: missing Teams service_url/conversation_id in context metadata"
        )
        return {
            "status": "error",
            "message": "Cannot send confirmation card: missing Teams channel context.",
        }

    # Query pipelines and stages to resolve default names for the preview
    default_pipeline_name: str | None = None
    default_pipeline_id: str | None = None
    default_stage_name: str | None = None

    pipeline_c = cast(Any, CRMPipeline).__table__.c
    stage_c = cast(Any, CRMDealStage).__table__.c

    async with context.acquire_session() as session:
        try:
            pipeline_stmt = (
                select(
                    col(CRMPipeline.pipeline_id),
                    col(CRMPipeline.name),
                    col(CRMPipeline.is_default),
                )
                .where(
                    pipeline_c.organisation_id == organisation_id,
                    pipeline_c.is_deleted.is_(False),
                    pipeline_c.is_active.is_(True),
                )
                .order_by(
                    pipeline_c.display_order.asc().nulls_last(),
                    pipeline_c.name.asc(),
                )
            )
            pipeline_result = await session.execute(pipeline_stmt)
            pipeline_rows = pipeline_result.all()

            for p in pipeline_rows:
                if p.is_default or default_pipeline_id is None:
                    default_pipeline_id = str(p.pipeline_id)
                    default_pipeline_name = p.name

            # Get first stage of default pipeline for preview display
            if default_pipeline_id:
                stage_stmt = (
                    select(
                        col(CRMDealStage.stage_id),
                        col(CRMDealStage.name),
                    )
                    .where(
                        stage_c.pipeline_id == UUID(default_pipeline_id),
                        stage_c.is_deleted.is_(False),
                    )
                    .order_by(
                        stage_c.display_order.asc().nulls_last(),
                        stage_c.name.asc(),
                    )
                    .limit(1)
                )
                stage_result = await session.execute(stage_stmt)
                first_stage = stage_result.first()
                if first_stage:
                    default_stage_name = first_stage.name
        except Exception as e:
            logger.exception("create_deal_form: error querying pipelines/stages: %s", e)

    # Build preview card
    nonce = create_nonce("deal_confirm")
    em = "\u2014"  # em dash for empty fields

    amount_display = em
    if amount is not None:
        curr = (currency or "USD").upper()
        amount_display = f"{curr} {amount:,.2f}"

    facts = [
        {"title": "Name", "value": name},
        {"title": "Amount", "value": amount_display},
        {"title": "Pipeline", "value": default_pipeline_name or em},
        {"title": "Stage", "value": default_stage_name or em},
        {"title": "Close Date", "value": close_date or em},
        {
            "title": "Priority",
            "value": (priority or em).capitalize() if priority else em,
        },
        {"title": "Account", "value": account_name or em},
        {"title": "Description", "value": description or em},
    ]

    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": "Create New Deal",
            "size": "Medium",
            "weight": "Bolder",
            "wrap": True,
        },
        {"type": "FactSet", "facts": facts},
        {
            "type": "TextBlock",
            "text": "Please confirm, edit, or cancel this deal creation.",
            "size": "Small",
            "isSubtle": True,
            "wrap": True,
            "spacing": "Medium",
        },
    ]

    # All deal data embedded in button payloads
    deal_data: dict[str, Any] = {
        "nonce": nonce,
        "organisation_id": str(organisation_id),
        "name": name,
        "amount": amount,
        "currency": (currency or "USD").upper(),
        "description": description or "",
        "close_date": close_date or "",
        "priority": priority or "",
        "account_name": account_name or "",
        "account_domain": account_domain or "",
        "pipeline_id": default_pipeline_id or "",
    }

    cancel_data: dict[str, Any] = {
        "nonce": nonce,
        "organisation_id": str(organisation_id),
    }

    confirm_fallback = {"action": "create_deal_submit", **deal_data}
    edit_fallback = {"action": "create_deal_edit", **deal_data}
    cancel_fallback = {"action": "create_deal_cancel", **cancel_data}

    actions = [
        {
            "type": "Action.Execute",
            "title": "Confirm",
            "style": "positive",
            "verb": "create_deal_submit",
            "data": deal_data,
            "fallback": {
                "type": "Action.Submit",
                "title": "Confirm",
                "style": "positive",
                "data": confirm_fallback,
            },
        },
        {
            "type": "Action.Execute",
            "title": "Edit",
            "verb": "create_deal_edit",
            "data": dict(deal_data),
            "fallback": {
                "type": "Action.Submit",
                "title": "Edit",
                "data": edit_fallback,
            },
        },
        {
            "type": "Action.Execute",
            "title": "Cancel",
            "style": "destructive",
            "verb": "create_deal_cancel",
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

        confirm_value = json.dumps(deal_data, separators=(",", ":"))
        cancel_value = json.dumps(cancel_data, separators=(",", ":"))
        description_value = description or em
        slack_fields = [
            _slack_field("Name", name),
            _slack_field("Amount", amount_display),
            _slack_field("Pipeline", default_pipeline_name or em),
            _slack_field("Stage", default_stage_name or em),
            _slack_field("Close Date", close_date or em),
            _slack_field("Priority", (priority or em).capitalize() if priority else em),
            _slack_field("Account", account_name or em),
            _slack_field("Domain", account_domain or em),
        ]
        slack_blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "Create New Deal"},
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Review the details below before creating the deal.",
                    }
                ],
            },
            {"type": "divider"},
            {
                "type": "section",
                "fields": slack_fields,
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Description*\n{_slack_quote(str(description_value))}",
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Click *Confirm* to create the deal or *Cancel* to discard.",
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
                        "action_id": "create_deal_submit",
                        "value": confirm_value,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Cancel"},
                        "style": "danger",
                        "action_id": "create_deal_cancel",
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
            text_fallback=f"Deal creation preview for {name}",
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

        # Send the card
        bot_service = TeamsBotService()
        sent = await bot_service.send_proactive_payload(
            service_url=service_url,
            conversation_id=conversation_id,
            payload=card_payload,
            text_fallback="Deal creation preview",
        )

    if sent:
        return {
            "status": "preview_sent",
            "message": (
                "I've sent a deal creation preview to your chat. "
                "Please review the details and click **Confirm** to create it"
                + (", **Edit** to modify the details," if channel != "slack" else "")
                + " or **Cancel** to discard."
            ),
        }
    else:
        logger.error("create_deal_form: failed to send proactive payload")
        return {
            "status": "error",
            "message": "Failed to send the deal creation preview. Please try again.",
        }


def create_create_deal(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the create_deal tool bound to a context.

    SECURITY: organisation_id comes from context (immutable, not a tool parameter).
    """

    async def create_deal_tool(
        name: str,
        amount: float | None = None,
        currency: str | None = None,
        description: str | None = None,
        close_date: str | None = None,
        priority: str | None = None,
        account_name: str | None = None,
        account_domain: str | None = None,
    ) -> dict[str, Any] | None:
        """Send a deal creation confirmation preview to the user's chat.

        Use this tool when the user wants to create a new deal. Extract deal
        details from the conversation and pass them as pre-filled values.
        A confirmation preview will be sent for the user to review and confirm.
        If only the deal name is known, still call this tool immediately.
        Do not ask follow-up questions for optional fields like account/company
        before sending the preview.

        name is REQUIRED. If the user says "create a deal" without providing a
        name, ask them for the deal name first before calling this tool.

        Args:
            name: Deal name (REQUIRED, e.g. "Acme Corp - Enterprise License")
            amount: Deal amount to pre-fill (e.g. 50000)
            currency: Currency code to pre-fill (e.g. "USD", "EUR"). Defaults to USD.
            description: Deal description to pre-fill
            close_date: Expected close date to pre-fill (YYYY-MM-DD format)
            priority: Priority level to pre-fill ("low", "medium", "high")
            account_name: Account/company name to pre-fill
            account_domain: Account website domain to pre-fill (e.g. "acme.com")

        Returns:
            Status dict indicating whether the confirmation preview was sent.
        """
        logger.info(
            "[TOOL CALLED] create_deal - name='%s', amount=%s, currency='%s'",
            name,
            amount,
            currency,
        )

        if not context.organisation_id:
            logger.warning("create_deal unavailable: missing organisation_id")
            return None

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="CreateDeal",
            tool_func=create_deal_form,
            parameters={
                "context": context,
                "name": name,
                "amount": amount,
                "currency": currency,
                "description": description,
                "close_date": close_date,
                "priority": priority,
                "account_name": account_name,
                "account_domain": account_domain,
            },
            description="Creating deal",
            fallback_value=None,
        )

        if result:
            logger.info(
                "[TOOL RESULT] create_deal status=%s",
                result.get("status"),
            )
        else:
            logger.info("[TOOL RESULT] create_deal returned None (error)")

        return dict(result) if result else None

    return create_deal_tool


__all__ = [
    "create_deal_form",
    "create_create_deal",
]
