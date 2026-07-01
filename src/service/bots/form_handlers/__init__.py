"""Form handler registry for processing interactive card submissions.

Routes form actions (e.g. "create_deal_submit") to the appropriate handler.
New form actions can be registered by adding a handler class and calling
FormHandlerRegistry.register().
"""

from __future__ import annotations

import logging
from typing import Any

from src.service.bots.form_handlers.add_deal_note_handler import AddDealNoteFormHandler
from src.service.bots.form_handlers.base import (
    BaseFormHandler,
    CancelFormHandler,
    FormHandlerResult,
    NonceCancelFormHandler,
)
from src.service.bots.form_handlers.cancel_reminder_handler import CancelReminderFormHandler
from src.service.bots.form_handlers.confirm_reminder_handler import (
    CancelPendingReminderFormHandler,
    ConfirmReminderFormHandler,
)
from src.service.bots.form_handlers.confirm_task_handler import ConfirmTaskFormHandler
from src.service.bots.form_handlers.create_deal_edit_handler import CreateDealEditFormHandler
from src.service.bots.form_handlers.create_deal_handler import CreateDealFormHandler
from src.service.bots.form_handlers.update_deal_stage_handler import UpdateDealStageFormHandler

logger = logging.getLogger(__name__)


class FormHandlerRegistry:
    """Registry that routes form submission actions to handlers.

    Usage:
        result = await FormHandlerRegistry.handle("create_deal_submit", activity, form_data)
        if result:
            # Send result.message back to user
    """

    _handlers: dict[str, BaseFormHandler] = {}

    @classmethod
    def register(cls, action: str, handler: BaseFormHandler) -> None:
        """Register a handler for a form action.

        Args:
            action: Action identifier (e.g. "create_deal_submit")
            handler: Handler instance to process the action
        """
        cls._handlers[action] = handler
        logger.debug("Registered form handler for action: %s", action)

    @classmethod
    async def handle(
        cls,
        action: str,
        activity: dict[str, Any],
        form_data: dict[str, Any],
    ) -> FormHandlerResult | None:
        """Route a form submission to the appropriate handler.

        Args:
            action: Action identifier from the submitted form data
            activity: Full Bot Framework activity
            form_data: Submitted form field values + metadata

        Returns:
            FormHandlerResult if a handler was found, None otherwise
        """
        handler = cls._handlers.get(action)
        if not handler:
            return None

        logger.info("Handling form action: %s", action)
        return await handler.handle(activity, form_data)


# Register handlers on import
FormHandlerRegistry.register("create_deal_submit", CreateDealFormHandler())
FormHandlerRegistry.register("create_deal_edit", CreateDealEditFormHandler())
FormHandlerRegistry.register(
    "create_deal_cancel",
    NonceCancelFormHandler("deal_confirm", "Deal creation cancelled."),
)
FormHandlerRegistry.register("add_deal_note_submit", AddDealNoteFormHandler())
FormHandlerRegistry.register(
    "add_deal_note_cancel",
    NonceCancelFormHandler("note_confirm", "Note cancelled."),
)
FormHandlerRegistry.register("update_deal_stage_submit", UpdateDealStageFormHandler())
FormHandlerRegistry.register(
    "update_deal_stage_cancel", CancelFormHandler("Stage update cancelled.")
)
FormHandlerRegistry.register("cancel_reminder_submit", CancelReminderFormHandler())
FormHandlerRegistry.register(
    "cancel_reminder_cancel", CancelFormHandler("Reminder cancellation dismissed.")
)
FormHandlerRegistry.register("confirm_reminder_submit", ConfirmReminderFormHandler())
FormHandlerRegistry.register("confirm_reminder_cancel", CancelPendingReminderFormHandler())
FormHandlerRegistry.register("confirm_task_submit", ConfirmTaskFormHandler())
FormHandlerRegistry.register("confirm_task_cancel", CancelFormHandler("Task creation cancelled."))


__all__ = [
    "BaseFormHandler",
    "CancelFormHandler",
    "FormHandlerRegistry",
    "FormHandlerResult",
    "NonceCancelFormHandler",
]
