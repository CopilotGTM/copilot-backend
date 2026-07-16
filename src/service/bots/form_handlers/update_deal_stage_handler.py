"""Form handler for deal stage update submissions.

Processes "update_deal_stage_submit" actions from interactive cards,
validates the form data, and moves the deal to the new stage via DealWriteService.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from src.infra.utils.logger import get_logger
from src.service.bots.form_handlers.base import BaseFormHandler, FormHandlerResult
from src.service.bots.form_handlers.identity import resolve_user_from_activity

logger = get_logger(__name__)


class UpdateDealStageFormHandler(BaseFormHandler):
    """Handle update_deal_stage_submit form submissions."""

    async def handle(
        self,
        activity: dict[str, Any],
        form_data: dict[str, Any],
    ) -> FormHandlerResult:
        """Process a deal stage update form submission.

        Extracts the selected stage, resolves the submitting user,
        and moves the deal to the new stage via DealWriteService.
        """
        # Extract form fields
        stage_id_raw = form_data.get("stage_id", "")
        if not stage_id_raw:
            return FormHandlerResult(
                success=False,
                message="Stage selection is required. Please try again.",
            )

        try:
            stage_id = UUID(stage_id_raw)
        except ValueError:
            logger.warning("update_deal_stage_handler: invalid stage_id: %s", stage_id_raw)
            return FormHandlerResult(
                success=False,
                message="Invalid stage selection. Please try again.",
            )

        deal_id_raw = form_data.get("deal_id", "")
        deal_name = form_data.get("deal_name", "Unknown Deal")
        organisation_id_str = form_data.get("organisation_id", "")

        try:
            deal_id = UUID(deal_id_raw)
        except ValueError:
            logger.warning("update_deal_stage_handler: invalid deal_id: %s", deal_id_raw)
            return FormHandlerResult(
                success=False,
                message="Invalid deal reference. Please try again.",
            )

        try:
            from src.infra.database.sessions import (
                acquire_master_async_session,
                acquire_org_async_session,
            )

            # Look up user
            user, _ = await resolve_user_from_activity(activity)

            if not user:
                return FormHandlerResult(
                    success=False,
                    message="Could not identify your account. Please sign in first.",
                )

            # Use the user's organisation_id (security: don't trust form metadata alone)
            org_id = user.organisation_id
            if not org_id:
                return FormHandlerResult(
                    success=False,
                    message="No organisation found for your account.",
                )

            # Validate that organisation_id from form matches user's org
            if organisation_id_str:
                try:
                    form_org_id = UUID(organisation_id_str)
                    if form_org_id != org_id:
                        logger.warning(
                            "update_deal_stage_handler: org mismatch form=%s user=%s",
                            form_org_id,
                            org_id,
                        )
                except ValueError:
                    pass

            # Move the deal to the new stage
            from src.service.crm.deals.contracts import DealWriteServiceError
            from src.service.crm.deals.write_service import DealWriteService

            async for master_session in acquire_master_async_session():
                async for org_session in acquire_org_async_session(org_id):
                    try:
                        write_service = DealWriteService(
                            org_session=org_session,
                            master_session=master_session,
                            organisation_id=org_id,
                            actor_user_id=user.user_id,
                        )

                        deal = await write_service.move_stage(deal_id, stage_id)
                        await org_session.commit()

                        logger.info(
                            "Deal stage updated via form: deal_id=%s stage='%s' org=%s user=%s",
                            deal.deal_id,
                            deal.stage_name,
                            org_id,
                            user.user_id,
                        )

                        return FormHandlerResult(
                            success=True,
                            message=(
                                f"Deal **{deal_name}** moved to stage **{deal.stage_name}**! "
                                f"Deal ID: {deal.deal_id}."
                            ),
                        )

                    except DealWriteServiceError as e:
                        logger.warning(
                            "update_deal_stage_handler: DealWriteServiceError: %s",
                            e.message,
                        )
                        return FormHandlerResult(
                            success=False,
                            message=f"Could not update stage: {e.message}",
                        )

                    except Exception as e:
                        logger.exception("update_deal_stage_handler: unexpected error: %s", e)
                        await org_session.rollback()
                        return FormHandlerResult(
                            success=False,
                            message="An unexpected error occurred while updating the stage.",
                        )

        except Exception as e:
            logger.exception("update_deal_stage_handler: failed to process form: %s", e)
            return FormHandlerResult(
                success=False,
                message="An error occurred while processing your request.",
            )

        # Fallback (shouldn't reach here)
        return FormHandlerResult(
            success=False,
            message="An unexpected error occurred.",
        )


__all__ = ["UpdateDealStageFormHandler"]
