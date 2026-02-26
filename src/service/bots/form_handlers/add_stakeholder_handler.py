"""Form handler for add-stakeholder submissions.

Processes "add_stakeholder_submit" actions from interactive cards,
validates the form data, and creates the stakeholder via StakeholdersService.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from src.infra.utils.logger import get_logger
from src.service.bots.form_handlers.base import BaseFormHandler, FormHandlerResult
from src.service.bots.form_handlers.identity import resolve_user_from_activity

logger = get_logger(__name__)


class AddStakeholderFormHandler(BaseFormHandler):
    """Handle add_stakeholder_submit form submissions."""

    async def handle(
        self,
        activity: dict[str, Any],
        form_data: dict[str, Any],
    ) -> FormHandlerResult:
        """Process a stakeholder form submission.

        Extracts stakeholder fields, resolves the submitting user,
        and creates the stakeholder in the database.
        """
        # Extract and validate email (required)
        email = (form_data.get("email") or "").strip().lower()
        if not email:
            return FormHandlerResult(
                success=False,
                message="Email is required. Please try again.",
            )

        # Extract optional fields
        name = (form_data.get("name") or "").strip() or None
        job_title = (form_data.get("job_title") or "").strip() or None
        stance = form_data.get("stance") or None
        decision_power = form_data.get("decision_power") or None

        # Extract deal metadata
        deal_id_str = form_data.get("deal_id", "")
        deal_name = form_data.get("deal_name", "")
        organisation_id_str = form_data.get("organisation_id", "")

        if not deal_id_str:
            return FormHandlerResult(
                success=False,
                message="Missing deal information. Please try again.",
            )

        try:
            deal_id = UUID(deal_id_str)
        except ValueError:
            return FormHandlerResult(
                success=False,
                message="Invalid deal information. Please try again.",
            )

        try:
            from src.infra.database.sessions import acquire_org_async_session

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
                            "add_stakeholder_handler: org mismatch form=%s user=%s",
                            form_org_id,
                            org_id,
                        )
                except ValueError:
                    pass

            # Build the stakeholder request
            from src.api.schemas.crm_models import CreateStakeholderRequest
            from src.domain.models.organisation_tables.sync.crm.stakeholders import (
                DecisionPower as DecisionPowerEnum,
            )
            from src.domain.models.organisation_tables.sync.crm.stakeholders import (
                StakeholderStance,
            )

            request = CreateStakeholderRequest(
                email=email,
                name=name,
                job_title=job_title,
                stance=(StakeholderStance(stance) if stance else StakeholderStance.UNKNOWN),
                decision_power=(
                    DecisionPowerEnum(decision_power)
                    if decision_power
                    else DecisionPowerEnum.UNKNOWN
                ),
            )

            # Create the stakeholder
            from src.service.crm.insights.stakeholders_service import (
                StakeholdersService,
                StakeholdersServiceError,
            )

            async for org_session in acquire_org_async_session(org_id):
                try:
                    service = StakeholdersService(
                        org_session=org_session,
                        organisation_id=org_id,
                    )

                    await service.create_stakeholder(
                        deal_id=deal_id,
                        request=request,
                    )
                    # StakeholdersService.create_stakeholder commits internally

                    display_name = name or email
                    logger.info(
                        "Stakeholder added via form: deal_id=%s deal='%s' email=%s org=%s user=%s",
                        deal_id,
                        deal_name,
                        email,
                        org_id,
                        user.user_id,
                    )

                    return FormHandlerResult(
                        success=True,
                        message=(f"Stakeholder **{display_name}** added to deal **{deal_name}**!"),
                    )

                except StakeholdersServiceError as e:
                    logger.warning(
                        "add_stakeholder_handler: service error: %s (status=%s)",
                        e.message,
                        e.status_code,
                    )
                    if e.status_code == 409:
                        return FormHandlerResult(
                            success=False,
                            message=("A stakeholder with this email already exists for this deal."),
                        )
                    return FormHandlerResult(
                        success=False,
                        message=f"Could not add stakeholder: {e.message}",
                    )

                except Exception as e:
                    logger.exception("add_stakeholder_handler: unexpected error: %s", e)
                    await org_session.rollback()
                    return FormHandlerResult(
                        success=False,
                        message="An unexpected error occurred while adding the stakeholder.",
                    )

        except Exception as e:
            logger.exception("add_stakeholder_handler: failed to process form: %s", e)
            return FormHandlerResult(
                success=False,
                message="An error occurred while processing your request.",
            )

        # Fallback (shouldn't reach here)
        return FormHandlerResult(
            success=False,
            message="An unexpected error occurred.",
        )


__all__ = ["AddStakeholderFormHandler"]
