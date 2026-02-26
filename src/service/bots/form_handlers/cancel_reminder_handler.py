"""Form handler for cancelling a reminder from an interactive Adaptive Card.

When the list_reminders tool renders reminders as cards with Cancel buttons,
the button submits a form with action="cancel_reminder_submit" and the
reminder_id in the form data. This handler processes that submission.

Security: Resolves user from Teams identity headers, not form metadata.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.reminder_models import (
    ReminderStatus,
    UserReminder,
)
from src.infra.database.sessions import acquire_org_async_session
from src.service.bots.form_handlers.base import BaseFormHandler, FormHandlerResult
from src.service.bots.form_handlers.identity import resolve_user_from_activity

logger = logging.getLogger(__name__)


class CancelReminderFormHandler(BaseFormHandler):
    """Handle cancel_reminder_submit form submissions from Adaptive Cards."""

    async def handle(
        self,
        activity: dict[str, Any],
        form_data: dict[str, Any],
    ) -> FormHandlerResult:
        reminder_id_str = form_data.get("reminder_id", "")
        if not reminder_id_str:
            return FormHandlerResult(success=False, message="Missing reminder ID.")

        try:
            reminder_id = UUID(reminder_id_str)
        except ValueError:
            return FormHandlerResult(success=False, message="Invalid reminder ID.")

        user, _ = await resolve_user_from_activity(activity)

        if not user:
            return FormHandlerResult(
                success=False,
                message="Could not identify your account.",
            )

        org_id = user.organisation_id
        user_id = user.user_id

        # Cancel the reminder (only if owned by this user)
        cancelled_title: str | None = None
        async for session in acquire_org_async_session(org_id):
            stmt = select(UserReminder).where(
                col(UserReminder.reminder_id) == reminder_id,
                col(UserReminder.organisation_id) == org_id,
                col(UserReminder.user_id) == user_id,
            )
            result = await session.execute(stmt)
            reminder = result.scalars().first()

            if not reminder:
                return FormHandlerResult(
                    success=False,
                    message="Reminder not found or not owned by you.",
                )

            if reminder.status == ReminderStatus.CANCELLED:
                return FormHandlerResult(
                    success=True,
                    message="This reminder was already cancelled.",
                )

            reminder.status = ReminderStatus.CANCELLED
            session.add(reminder)
            await session.commit()
            cancelled_title = reminder.title

        if cancelled_title is None:
            return FormHandlerResult(success=False, message="Database error.")

        from src.infra.messaging.reminder_schedule import unschedule_reminder

        try:
            unschedule_reminder(org_id, reminder_id)
        except Exception:
            logger.warning(
                "Failed to ZREM reminder %s; poller will skip it (DB status is CANCELLED)",
                reminder_id,
                exc_info=True,
            )

        return FormHandlerResult(
            success=True,
            message=f"Reminder **{cancelled_title}** has been cancelled.",
        )
