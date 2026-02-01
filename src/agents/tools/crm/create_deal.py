"""Create Deal tool - sends an interactive deal creation form to the user's chat channel.

This tool builds a channel-agnostic FormDefinition with deal fields
(pre-filled from agent-extracted values), renders it via the appropriate
card builder, and sends it as a proactive message to the user's chat.

The actual deal creation happens when the user submits the form,
handled by CreateDealFormHandler in src/service/bots/form_handlers/.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access

This is an ORG-SCOPED tool. deal_id is NOT required.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select
from sqlmodel import col

from src.adapters.card_builders import FormDefinition, FormField, get_card_builder
from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealStage,
    CRMPipeline,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


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
    """Build and send an interactive deal creation form to the user's chat channel.

    Queries pipelines/stages for dropdown choices, builds a FormDefinition,
    renders it for the target channel, and sends via proactive messaging.

    Args:
        context: AgentContext with organisation_id and channel metadata
        name: Pre-filled deal name
        amount: Pre-filled deal amount
        currency: Pre-filled currency code (e.g. "USD")
        description: Pre-filled deal description
        close_date: Pre-filled close date (YYYY-MM-DD)
        priority: Pre-filled priority level
        account_name: Pre-filled account/company name
        account_domain: Pre-filled account domain (e.g. "acme.com")

    Returns:
        Dict with status and message, or None on error
    """
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("create_deal_form: missing organisation_id in context")
        return None

    # Verify channel metadata is available for sending the card
    service_url = context.metadata.get("service_url", "")
    conversation_id = context.metadata.get("conversation_id", "")

    if not service_url or not conversation_id:
        logger.warning(
            "create_deal_form: missing service_url or conversation_id in context metadata"
        )
        return {
            "status": "error",
            "message": (
                "Cannot send interactive form: missing channel context. "
                "This tool is only available in Teams chat."
            ),
        }

    # Query pipelines and stages for dropdown choices
    pipeline_choices: list[dict[str, str]] = []
    stage_choices: list[dict[str, str]] = []
    default_pipeline_id: str | None = None

    pipeline_c = cast(Any, CRMPipeline).__table__.c
    stage_c = cast(Any, CRMDealStage).__table__.c

    async with context.acquire_session() as session:
        try:
            # Fetch active pipelines
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
                pid = str(p.pipeline_id)
                pipeline_choices.append({"title": p.name, "value": pid})
                if p.is_default or default_pipeline_id is None:
                    default_pipeline_id = pid

            # Fetch stages for all pipelines (agent-side, user picks pipeline+stage)
            if pipeline_rows:
                pipeline_ids = [p.pipeline_id for p in pipeline_rows]
                stage_stmt = (
                    select(
                        col(CRMDealStage.stage_id),
                        col(CRMDealStage.pipeline_id),
                        col(CRMDealStage.name),
                    )
                    .where(
                        stage_c.pipeline_id.in_(pipeline_ids),
                        stage_c.is_deleted.is_(False),
                    )
                    .order_by(
                        stage_c.display_order.asc().nulls_last(),
                        stage_c.name.asc(),
                    )
                )
                stage_result = await session.execute(stage_stmt)
                stage_rows = stage_result.all()

                # Prefix stage names with pipeline name when multiple pipelines
                # exist, to disambiguate identical stage names across pipelines
                pipeline_names: dict[str, str] = {str(p.pipeline_id): p.name for p in pipeline_rows}
                multiple_pipelines = len(pipeline_rows) > 1

                for s in stage_rows:
                    title = (
                        f"{pipeline_names[str(s.pipeline_id)]} > {s.name}"
                        if multiple_pipelines
                        else s.name
                    )
                    stage_choices.append(
                        {
                            "title": title,
                            "value": f"{s.pipeline_id}|{s.stage_id}",
                        }
                    )

        except Exception as e:
            logger.exception("create_deal_form: error querying pipelines/stages: %s", e)
            # Continue with empty choices - form still useful with text fields

    # Build the form definition
    fields: list[FormField] = [
        FormField(
            id="name",
            label="Deal Name",
            field_type="text",
            required=True,
            value=name,
            placeholder="e.g. Acme Corp - Enterprise License",
        ),
        FormField(
            id="amount",
            label="Amount",
            field_type="number",
            value=amount,
            placeholder="e.g. 50000",
            min_val=0,
        ),
        FormField(
            id="currency",
            label="Currency",
            field_type="text",
            value=currency or "USD",
            placeholder="e.g. USD, EUR, GBP",
        ),
    ]

    # Pipeline dropdown (if pipelines exist)
    if pipeline_choices:
        fields.append(
            FormField(
                id="pipeline_id",
                label="Pipeline",
                field_type="choice",
                choices=pipeline_choices,
                value=default_pipeline_id,
            )
        )

    # Stage dropdown (if stages exist)
    if stage_choices:
        fields.append(
            FormField(
                id="stage",
                label="Stage",
                field_type="choice",
                choices=stage_choices,
                placeholder="Select a stage",
            )
        )

    fields.extend(
        [
            FormField(
                id="close_date",
                label="Expected Close Date",
                field_type="date",
                value=close_date,
            ),
            FormField(
                id="priority",
                label="Priority",
                field_type="choice",
                choices=[
                    {"title": "Low", "value": "low"},
                    {"title": "Medium", "value": "medium"},
                    {"title": "High", "value": "high"},
                ],
                value=priority,
                placeholder="Select priority",
            ),
            FormField(
                id="account_name",
                label="Account Name",
                field_type="text",
                value=account_name,
                placeholder="e.g. Acme Corp",
            ),
            FormField(
                id="account_domain",
                label="Account Domain",
                field_type="text",
                value=account_domain,
                placeholder="e.g. acme.com",
            ),
            FormField(
                id="description",
                label="Description",
                field_type="text",
                value=description,
                placeholder="Brief description of the deal",
                multiline=True,
            ),
        ]
    )

    form = FormDefinition(
        title="Create New Deal",
        subtitle="Fill in the deal details below. Required fields are marked with *.",
        fields=fields,
        submit_action="create_deal_submit",
        submit_label="Create Deal",
        cancel_action="create_deal_cancel",
        cancel_label="Cancel",
        metadata={
            "organisation_id": str(organisation_id),
        },
    )

    # Build the card for the target channel
    try:
        builder = get_card_builder(context.channel)
        card_payload = builder.build(form)
    except ValueError:
        logger.warning("create_deal_form: no card builder for channel=%s", context.channel)
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
        text_fallback="Deal creation form",
    )

    if sent:
        return {
            "status": "form_sent",
            "message": "I've sent a deal creation form to your chat. Please fill it out and submit.",
        }
    else:
        logger.error("create_deal_form: failed to send proactive payload")
        return {
            "status": "error",
            "message": "Failed to send the deal creation form. Please try again.",
        }


def create_create_deal(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the create_deal tool bound to a context.

    SECURITY: organisation_id comes from context (immutable, not a tool parameter).
    """

    async def create_deal_tool(
        name: str | None = None,
        amount: float | None = None,
        currency: str | None = None,
        description: str | None = None,
        close_date: str | None = None,
        priority: str | None = None,
        account_name: str | None = None,
        account_domain: str | None = None,
    ) -> dict[str, Any] | None:
        """Send an interactive deal creation form to the user's chat.

        Use this tool when the user wants to create a new deal. Extract any deal
        details mentioned in the conversation and pass them as pre-filled values.
        The form will be sent to the user's chat for them to review and submit.

        All parameters are optional. If the user says "create a deal", send an
        empty form. If they say "create a deal named Acme for $50k", pre-fill
        name="Acme" and amount=50000.

        Args:
            name: Deal name to pre-fill (e.g. "Acme Corp - Enterprise License")
            amount: Deal amount to pre-fill (e.g. 50000)
            currency: Currency code to pre-fill (e.g. "USD", "EUR"). Defaults to USD.
            description: Deal description to pre-fill
            close_date: Expected close date to pre-fill (YYYY-MM-DD format)
            priority: Priority level to pre-fill ("low", "medium", "high")
            account_name: Account/company name to pre-fill
            account_domain: Account website domain to pre-fill (e.g. "acme.com")

        Returns:
            Status dict indicating whether the form was sent successfully.
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
            description="Sending deal creation form",
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
