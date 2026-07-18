"""Form handler for deal creation submissions.

Processes "create_deal_submit" actions from interactive cards,
validates the form data, and creates the deal via DealWriteService.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from src.agents.tools.crm.policy import ensure_crm_write_scope
from src.infra.utils.logger import get_logger
from src.service.bots.form_handlers.base import BaseFormHandler, FormHandlerResult
from src.service.bots.form_handlers.identity import resolve_user_from_activity
from src.service.bots.form_handlers.nonce import validate_and_consume_nonce
from src.service.orgnisation.organisation_policy_service import OrganisationPolicyDeniedError

logger = get_logger(__name__)


class CreateDealFormHandler(BaseFormHandler):
    """Handle create_deal_submit form submissions."""

    async def handle(
        self,
        activity: dict[str, Any],
        form_data: dict[str, Any],
    ) -> FormHandlerResult:
        """Process a deal creation form submission.

        Extracts form fields, resolves the submitting user,
        and creates the deal in the database.
        """
        # Validate one-time nonce to prevent duplicate/stale card submissions
        nonce = form_data.get("nonce", "")
        if not nonce or not validate_and_consume_nonce("deal_confirm", nonce):
            return FormHandlerResult(
                success=False,
                message=(
                    "This confirmation has expired or was already used. "
                    "Please ask the assistant to send a new one."
                ),
            )

        # Extract form fields
        deal_name = (form_data.get("name") or "").strip()
        if not deal_name:
            return FormHandlerResult(
                success=False,
                message="Deal name is required. Please try again.",
            )

        # Parse amount
        amount = None
        amount_raw = form_data.get("amount")
        if amount_raw:
            try:
                amount = float(amount_raw)
            except (ValueError, TypeError):
                logger.warning("create_deal_handler: invalid amount value: %s", amount_raw)

        currency = (form_data.get("currency") or "USD").strip().upper()
        description = (form_data.get("description") or "").strip() or None
        expected_close_date_str = form_data.get("expected_close_date")
        priority = (form_data.get("priority") or "").strip() or None
        account_name = (form_data.get("account_name") or "").strip() or None
        account_domain = (form_data.get("account_domain") or "").strip() or None

        # Parse pipeline_id and stage from form
        pipeline_id: UUID | None = None
        stage_id: UUID | None = None

        pipeline_id_raw = form_data.get("pipeline_id")
        if pipeline_id_raw:
            try:
                pipeline_id = UUID(pipeline_id_raw)
            except ValueError:
                logger.warning("create_deal_handler: invalid pipeline_id: %s", pipeline_id_raw)

        # Stage field contains "pipeline_id|stage_id" compound value
        stage_raw = form_data.get("stage")
        if stage_raw and "|" in stage_raw:
            try:
                _, stage_id_str = stage_raw.split("|", 1)
                stage_id = UUID(stage_id_str)
            except (ValueError, IndexError):
                logger.warning("create_deal_handler: invalid stage value: %s", stage_raw)

        # Parse forecast close date for open-pipeline planning.
        expected_close_date = None
        if expected_close_date_str:
            from datetime import datetime

            try:
                expected_close_date = datetime.fromisoformat(expected_close_date_str)
            except ValueError:
                logger.warning(
                    "create_deal_handler: invalid expected_close_date: %s",
                    expected_close_date_str,
                )

        organisation_id_str = form_data.get("organisation_id", "")

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
                            "create_deal_handler: org mismatch form=%s user=%s",
                            form_org_id,
                            org_id,
                        )
                except ValueError:
                    pass

            try:
                await ensure_crm_write_scope(org_id, "deals:create")
            except OrganisationPolicyDeniedError as exc:
                return FormHandlerResult(success=False, message=exc.message)

            # Form submission is the point where a preview becomes a CRM write;
            # keep the service imports local so read-only form handlers stay light.
            from src.service.crm.access.record_access_policy import CRMAccessActor
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
                            access_actor=CRMAccessActor(
                                user.user_id,
                                user.user_role,
                                str(user.email) if user.email else None,
                            ),
                        )

                        payload: dict[str, Any] = {
                            "name": deal_name,
                            "amount": amount,
                            "currency": currency,
                            "description": description,
                            "expected_close_date": expected_close_date,
                            "priority": priority,
                            "owner_user_id": user.user_id,
                        }

                        if pipeline_id:
                            payload["pipeline_id"] = pipeline_id
                        if stage_id:
                            payload["stage_id"] = stage_id

                        # Add account info if provided
                        if account_name or account_domain:
                            payload["account"] = {
                                "name": account_name,
                                "domain": account_domain,
                            }

                        deal = await write_service.create_deal(payload)
                        await org_session.commit()

                        logger.info(
                            "Deal created via form: deal_id=%s name='%s' org=%s user=%s",
                            deal.deal_id,
                            deal.name,
                            org_id,
                            user.user_id,
                        )

                        message = (
                            f"Deal **{deal.name}** created successfully! Deal ID: {deal.deal_id}."
                            + (f" Amount: {currency} {amount:,.2f}." if amount else "")
                        )
                        for label, value in (
                            ("Owner", getattr(deal, "owner_name", None)),
                            ("Status", getattr(deal, "deal_status", None)),
                            ("Pipeline", getattr(deal, "pipeline_name", None)),
                            ("Stage", getattr(deal, "stage_name", None)),
                        ):
                            if value:
                                message += f" {label}: {getattr(value, 'value', value)}."

                        return FormHandlerResult(success=True, message=message)

                    except DealWriteServiceError as e:
                        logger.warning("create_deal_handler: DealWriteServiceError: %s", e.message)
                        return FormHandlerResult(
                            success=False,
                            message=f"Could not create deal: {e.message}",
                        )

                    except Exception as e:
                        logger.exception("create_deal_handler: unexpected error: %s", e)
                        await org_session.rollback()
                        return FormHandlerResult(
                            success=False,
                            message="An unexpected error occurred while creating the deal.",
                        )

        except Exception as e:
            logger.exception("create_deal_handler: failed to process form: %s", e)
            return FormHandlerResult(
                success=False,
                message="An error occurred while processing your request.",
            )

        # Fallback (shouldn't reach here)
        return FormHandlerResult(
            success=False,
            message="An unexpected error occurred.",
        )


__all__ = ["CreateDealFormHandler"]
