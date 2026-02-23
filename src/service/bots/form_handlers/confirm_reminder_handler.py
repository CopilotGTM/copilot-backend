"""Form handler for confirming a pending reminder from a preview Adaptive Card.

When create_reminder sends a preview card with Confirm/Cancel buttons,
the Confirm button submits action="confirm_reminder_submit" with the
reminder_id in the form data. This handler flips the status from
pending_confirmation to active and ZADD's it into the Redis sorted set.

The Cancel button submits action="confirm_reminder_cancel" and is
handled by CancelFormHandler (deletes the pending reminder).

Security: Resolves user from Teams identity headers, not form metadata.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.master_tables.external_identity_models import (
    ExternalIdentityProvider,
)
from src.domain.models.organisation_tables.reminder_models import (
    ReminderStatus,
    UserReminder,
)
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.service.bots.form_handlers.base import BaseFormHandler, FormHandlerResult
from src.service.bots.identity_service import ExternalIdentityService

logger = logging.getLogger(__name__)


class ConfirmReminderFormHandler(BaseFormHandler):
    """Handle confirm_reminder_submit form submissions from preview cards."""

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

        # Resolve user from Teams identity
        from_user = activity.get("from", {})
        teams_user_id = from_user.get("id", "")
        channel_data = activity.get("channelData", {})
        teams_tenant_id = channel_data.get("tenant", {}).get("id", "")

        identity_service = ExternalIdentityService()
        async for master_session in acquire_master_async_session():
            user, _ = await identity_service.get_user_for_external_identity(
                master_session,
                ExternalIdentityProvider.TEAMS,
                teams_user_id,
                teams_tenant_id,
            )
            break

        if not user:
            return FormHandlerResult(
                success=False,
                message="Could not identify your account.",
            )

        org_id = user.organisation_id
        user_id = user.user_id

        # Activate the reminder (only if owned by this user and pending)
        confirmed_title: str | None = None
        trigger_ts: float | None = None

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

            if reminder.status == ReminderStatus.ACTIVE:
                return FormHandlerResult(
                    success=True,
                    message=f"Reminder **{reminder.title}** is already active.",
                )

            if reminder.status != ReminderStatus.PENDING_CONFIRMATION:
                return FormHandlerResult(
                    success=False,
                    message=f"Cannot confirm a reminder with status '{reminder.status}'.",
                )

            reminder.status = ReminderStatus.ACTIVE
            session.add(reminder)
            await session.commit()
            confirmed_title = reminder.title
            trigger_ts = reminder.next_trigger_at.timestamp()

        if confirmed_title is None:
            return FormHandlerResult(success=False, message="Database error.")

        # ZADD into Redis sorted set so the poller picks it up
        if trigger_ts is not None:
            from src.infra.messaging.reminder_schedule import schedule_reminder

            try:
                schedule_reminder(org_id, reminder_id, trigger_ts)
            except Exception:
                logger.warning(
                    "Failed to ZADD reminder %s; recovery sweep will pick it up",
                    reminder_id,
                    exc_info=True,
                )

        return FormHandlerResult(
            success=True,
            message=f"Reminder **{confirmed_title}** is now active!",
        )


class CancelPendingReminderFormHandler(BaseFormHandler):
    """Handle confirm_reminder_cancel — delete the pending reminder."""

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

        # Resolve user from Teams identity
        from_user = activity.get("from", {})
        teams_user_id = from_user.get("id", "")
        channel_data = activity.get("channelData", {})
        teams_tenant_id = channel_data.get("tenant", {}).get("id", "")

        identity_service = ExternalIdentityService()
        async for master_session in acquire_master_async_session():
            user, _ = await identity_service.get_user_for_external_identity(
                master_session,
                ExternalIdentityProvider.TEAMS,
                teams_user_id,
                teams_tenant_id,
            )
            break

        if not user:
            return FormHandlerResult(
                success=False,
                message="Could not identify your account.",
            )

        org_id = user.organisation_id
        user_id = user.user_id

        async for session in acquire_org_async_session(org_id):
            stmt = select(UserReminder).where(
                col(UserReminder.reminder_id) == reminder_id,
                col(UserReminder.organisation_id) == org_id,
                col(UserReminder.user_id) == user_id,
                col(UserReminder.status) == ReminderStatus.PENDING_CONFIRMATION,
            )
            result = await session.execute(stmt)
            reminder = result.scalars().first()

            if not reminder:
                return FormHandlerResult(
                    success=True,
                    message="Reminder already cancelled or not found.",
                )

            reminder.status = ReminderStatus.CANCELLED
            session.add(reminder)
            await session.commit()

        return FormHandlerResult(
            success=True,
            message="Reminder cancelled.",
        )
