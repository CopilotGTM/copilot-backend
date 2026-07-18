"""Update Deal Stage tool - sends an interactive stage selection form to the user's chat.

This tool queries the deal's current pipeline and its stages, builds a
FormDefinition with a single dropdown (current stage pre-selected), renders
it via the appropriate card builder, and sends it as a proactive message.

The LLM is responsible for resolving the deal first (via search_deals),
then calling this tool with a confirmed deal_id and deal_name.

The actual stage update happens when the user submits the form,
handled by UpdateDealStageFormHandler in src/service/bots/form_handlers/.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access

This is an ORG-SCOPED tool. deal_id is NOT required at init.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col

from src.adapters.card_builders import FormDefinition, FormField, get_card_builder
from src.agents.tools.base import execute_with_capabilities
from src.agents.tools.crm.access import AgentCRMAccessError, require_agent_deal_access
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealStage,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def update_deal_stage_form(
    *,
    context: AgentContext,
    deal_id: str,
    deal_name: str,
) -> dict[str, Any] | None:
    """Send an interactive stage selection form for a known deal.

    Queries the deal's pipeline, fetches its stages, and builds a single-dropdown
    form with the current stage pre-selected.

    Args:
        context: AgentContext with organisation_id and channel metadata
        deal_id: UUID string of the deal (already resolved by LLM)
        deal_name: Display name of the deal (already resolved by LLM)

    Returns:
        Dict with status and message, or None on error
    """
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("update_deal_stage_form: missing organisation_id in context")
        return None

    channel = (context.channel or "teams").lower()
    service_url = context.metadata.get("service_url", "")
    conversation_id = context.metadata.get("conversation_id", "")
    team_id = context.metadata.get("team_id", "")
    channel_id = context.metadata.get("channel_id", "")

    if channel == "slack":
        if not team_id or not channel_id:
            logger.warning("update_deal_stage_form: missing Slack team_id/channel_id in metadata")
            return {
                "status": "error",
                "message": "Cannot send interactive form: missing Slack channel context.",
            }
    elif not service_url or not conversation_id:
        logger.warning(
            "update_deal_stage_form: missing Teams service_url/conversation_id in metadata"
        )
        return {
            "status": "error",
            "message": "Cannot send interactive form: missing Teams channel context.",
        }

    # Parse deal_id
    try:
        deal_uuid = UUID(deal_id)
    except ValueError:
        logger.warning("update_deal_stage_form: invalid deal_id: %s", deal_id)
        return {
            "status": "error",
            "message": "Invalid deal ID format.",
        }

    # Query deal's pipeline and current stage, then fetch stages for that pipeline
    deal_c = cast(Any, CRMDeal).__table__.c
    stage_c = cast(Any, CRMDealStage).__table__.c

    stage_choices: list[dict[str, str]] = []
    current_stage_id: str | None = None

    async with context.acquire_session() as session:
        try:
            await require_agent_deal_access(context, session, deal_uuid, "control")
            # Get the deal's pipeline_id, current stage_id, and integration_id
            deal_stmt = select(
                col(CRMDeal.pipeline_id),
                col(CRMDeal.stage_id),
                col(CRMDeal.integration_id),
            ).where(
                deal_c.deal_id == deal_uuid,
                deal_c.organisation_id == organisation_id,
                deal_c.is_deleted.is_(False),
            )
            deal_result = await session.execute(deal_stmt)
            deal_row = deal_result.first()

            if not deal_row:
                return {
                    "status": "error",
                    "message": f"Deal **{deal_name}** not found.",
                }

            # Check if the deal is internal (only internal deals can be edited)
            from src.domain.models.organisation_tables.sync.integration_models import (
                IntegrationType,
                KnowledgeIntegration,
            )

            is_internal = False
            if deal_row.integration_id:
                int_stmt = select(col(KnowledgeIntegration.integration_type)).where(
                    cast(Any, KnowledgeIntegration).__table__.c.integration_id
                    == deal_row.integration_id,
                )
                int_result = await session.execute(int_stmt)
                int_row = int_result.first()
                if int_row:
                    is_internal = (
                        int_row.integration_type == IntegrationType.INTERNAL_CRM.value
                        or int_row.integration_type == IntegrationType.INTERNAL_CRM
                    )

            if not is_internal:
                return {
                    "status": "error",
                    "message": (
                        f"Deal **{deal_name}** is from an external CRM and is read-only. "
                        "Stage updates can only be made for internal deals."
                    ),
                }

            if not deal_row.pipeline_id:
                return {
                    "status": "error",
                    "message": (
                        f"Deal **{deal_name}** is not assigned to a pipeline. Cannot update stage."
                    ),
                }

            current_stage_id = str(deal_row.stage_id) if deal_row.stage_id else None

            # Fetch stages for the deal's pipeline
            stage_stmt = (
                select(
                    col(CRMDealStage.stage_id),
                    col(CRMDealStage.name),
                )
                .where(
                    stage_c.pipeline_id == deal_row.pipeline_id,
                    stage_c.is_deleted.is_(False),
                )
                .order_by(
                    stage_c.display_order.asc().nulls_last(),
                    stage_c.name.asc(),
                )
            )
            stage_result = await session.execute(stage_stmt)
            stage_rows = stage_result.all()

            if not stage_rows:
                return {
                    "status": "error",
                    "message": (f"No stages found for the pipeline of deal **{deal_name}**."),
                }

            for s in stage_rows:
                stage_choices.append({"title": s.name, "value": str(s.stage_id)})

        except AgentCRMAccessError:
            return {"status": "error", "message": "Deal not found."}
        except Exception as e:
            logger.exception("update_deal_stage_form: error querying deal/stages: %s", e)
            return {
                "status": "error",
                "message": "An error occurred while loading stage options.",
            }

    # Build the form definition
    fields: list[FormField] = [
        FormField(
            id="stage_id",
            label="Stage",
            field_type="choice",
            required=True,
            choices=stage_choices,
            value=current_stage_id,
            placeholder="Select a stage",
        ),
    ]

    form = FormDefinition(
        title="Update Deal Stage",
        subtitle=f"Deal: {deal_name}",
        fields=fields,
        submit_action="update_deal_stage_submit",
        submit_label="Update Stage",
        cancel_action="update_deal_stage_cancel",
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
        logger.warning("update_deal_stage_form: no card builder for channel=%s", context.channel)
        return {
            "status": "error",
            "message": f"Interactive forms are not supported on {context.channel}.",
        }

    # Send the card via the channel-specific bot service.
    if channel == "slack":
        from src.service.bots.slack_bot_service import SlackBotService

        slack_service = SlackBotService()
        blocks = card_payload.get("blocks")
        text_fallback = card_payload.get("text") or "Update deal stage form"
        if blocks:
            sent = await slack_service.send_block_kit_message(
                team_id=team_id,
                channel_id=channel_id,
                blocks=blocks,
                text_fallback=text_fallback,
            )
        else:
            sent = await slack_service._send_message(team_id, channel_id, text_fallback)
    else:
        from src.service.bots.teams_bot_service import TeamsBotService

        teams_service = TeamsBotService()
        sent = await teams_service.send_proactive_payload(
            service_url=service_url,
            conversation_id=conversation_id,
            payload=card_payload,
            text_fallback="Update deal stage form",
        )

    if sent:
        return {
            "status": "form_sent",
            "message": (
                f"I've sent a form to update the stage for deal **{deal_name}**. "
                "Please select the new stage and submit."
            ),
        }
    else:
        logger.error("update_deal_stage_form: failed to send proactive payload")
        return {
            "status": "error",
            "message": "Failed to send the stage update form. Please try again.",
        }


def create_update_deal_stage(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the update_deal_stage tool bound to a context.

    SECURITY: organisation_id comes from context (immutable, not a tool parameter).
    """

    async def update_deal_stage_tool(
        deal_id: str,
        deal_name: str,
    ) -> dict[str, Any] | None:
        """Send an interactive form to update the stage of an existing deal.

        IMPORTANT: Before calling this tool, you MUST first use search_deals to
        find the deal and confirm it with the user. Do NOT call this tool without
        a confirmed deal_id.

        Workflow:
        1. User asks to move/update a deal's stage
        2. You call search_deals to find matching deals
        3. If multiple matches, ask user which deal they mean
        4. Once confirmed, call this tool with the deal_id and deal_name
        5. The form will show stages from the deal's pipeline with the current
           stage pre-selected

        After the form is sent successfully, respond with a brief confirmation
        only. Do not add extra suggestions.

        Args:
            deal_id: UUID of the deal (from search_deals result)
            deal_name: Name of the deal (from search_deals result)

        Returns:
            Status dict indicating whether the form was sent successfully.
        """
        logger.info(
            "[TOOL CALLED] update_deal_stage - deal_id='%s', deal_name='%s'",
            deal_id,
            deal_name,
        )

        if not context.organisation_id:
            logger.warning("update_deal_stage unavailable: missing organisation_id")
            return None

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="UpdateDealStage",
            tool_func=update_deal_stage_form,
            parameters={
                "context": context,
                "deal_id": deal_id,
                "deal_name": deal_name,
            },
            description="Updating deal stage",
            fallback_value=None,
        )

        if result:
            logger.info(
                "[TOOL RESULT] update_deal_stage status=%s",
                result.get("status"),
            )
        else:
            logger.info("[TOOL RESULT] update_deal_stage returned None (error)")

        return dict(result) if result else None

    return update_deal_stage_tool


__all__ = [
    "update_deal_stage_form",
    "create_update_deal_stage",
]
