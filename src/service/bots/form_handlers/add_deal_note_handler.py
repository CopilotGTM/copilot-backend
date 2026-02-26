"""Form handler for add-deal-note submissions.

Processes "add_deal_note_submit" actions from interactive cards,
validates the form data, and creates the note via DealNotesService.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from src.infra.utils.logger import get_logger
from src.service.bots.form_handlers.base import BaseFormHandler, FormHandlerResult
from src.service.bots.form_handlers.identity import resolve_user_from_activity
from src.service.bots.form_handlers.nonce import validate_and_consume_nonce

logger = get_logger(__name__)


class AddDealNoteFormHandler(BaseFormHandler):
    """Handle add_deal_note_submit form submissions."""

    async def handle(
        self,
        activity: dict[str, Any],
        form_data: dict[str, Any],
    ) -> FormHandlerResult:
        """Process a deal note form submission.

        Extracts note content, resolves the submitting user,
        and creates the note in the database.
        """
        # Validate one-time nonce
        nonce = form_data.get("nonce", "")
        if not nonce or not validate_and_consume_nonce("note_confirm", nonce):
            return FormHandlerResult(
                success=False,
                message=(
                    "This confirmation has expired or was already used. "
                    "Please ask the assistant to send a new one."
                ),
            )

        # Extract and validate note content
        note_content = (form_data.get("note_content") or "").strip()
        if not note_content:
            return FormHandlerResult(
                success=False,
                message="Note content is required. Please try again.",
            )

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
                            "add_deal_note_handler: org mismatch form=%s user=%s",
                            form_org_id,
                            org_id,
                        )
                except ValueError:
                    pass

            # Create the note
            from src.service.crm.internal.deal_notes_service import DealNotesService

            async for org_session in acquire_org_async_session(org_id):
                try:
                    notes_service = DealNotesService(
                        org_session=org_session,
                        organisation_id=org_id,
                    )

                    create_result = await notes_service.create_user_note(
                        deal_id=deal_id,
                        content=note_content,
                        subject=None,
                        user_id=user.user_id,
                        user_name=f"{user.first_name} {user.last_name}",
                        user_email=user.email,
                    )
                    await org_session.commit()

                    if create_result.created_new:
                        logger.info(
                            "Note added via form: deal_id=%s deal='%s' org=%s user=%s",
                            deal_id,
                            deal_name,
                            org_id,
                            user.user_id,
                        )
                        return FormHandlerResult(
                            success=True,
                            message=f"Note added to deal **{deal_name}**!",
                        )

                    logger.info(
                        "Duplicate form note ignored: deal_id=%s deal='%s' org=%s user=%s",
                        deal_id,
                        deal_name,
                        org_id,
                        user.user_id,
                    )
                    return FormHandlerResult(
                        success=True,
                        message=f"Identical note already exists for **{deal_name}**.",
                    )

                except Exception as e:
                    logger.exception("add_deal_note_handler: unexpected error: %s", e)
                    await org_session.rollback()
                    return FormHandlerResult(
                        success=False,
                        message="An unexpected error occurred while adding the note.",
                    )

        except Exception as e:
            logger.exception("add_deal_note_handler: failed to process form: %s", e)
            return FormHandlerResult(
                success=False,
                message="An error occurred while processing your request.",
            )

        # Fallback (shouldn't reach here)
        return FormHandlerResult(
            success=False,
            message="An unexpected error occurred.",
        )


__all__ = ["AddDealNoteFormHandler"]
