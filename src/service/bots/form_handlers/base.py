"""Base classes for form submission handlers.

Form handlers process data submitted from interactive cards (Teams Adaptive Cards,
Slack Block Kit, etc.) and perform the corresponding business logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class FormHandlerResult:
    """Result of processing a form submission.

    Attributes:
        success: Whether the form was processed successfully
        message: Human-readable message to send back to the user
        card_payload: Optional rich card response (e.g. Adaptive Card)
    """

    success: bool
    message: str
    card_payload: dict[str, Any] | None = None


class BaseFormHandler(ABC):
    """Abstract base class for form submission handlers.

    Each form action (e.g. "create_deal_submit", "add_note_submit")
    gets its own handler that knows how to process the submitted data.
    """

    @abstractmethod
    async def handle(
        self,
        activity: dict[str, Any],
        form_data: dict[str, Any],
    ) -> FormHandlerResult:
        """Process a form submission.

        Args:
            activity: The full Bot Framework activity (contains user info, conversation context)
            form_data: The submitted form data (field values + action metadata)

        Returns:
            FormHandlerResult with success status and response message
        """
        ...


class CancelFormHandler(BaseFormHandler):
    """Generic handler for form cancellation actions."""

    def __init__(self, message: str = "Action cancelled."):
        self._message = message

    async def handle(
        self,
        activity: dict[str, Any],  # noqa: ARG002
        form_data: dict[str, Any],  # noqa: ARG002
    ) -> FormHandlerResult:
        return FormHandlerResult(success=True, message=self._message)


class NonceCancelFormHandler(BaseFormHandler):
    """Cancel handler that also consumes the Redis nonce."""

    def __init__(self, nonce_prefix: str, message: str = "Action cancelled."):
        self._nonce_prefix = nonce_prefix
        self._message = message

    async def handle(
        self,
        activity: dict[str, Any],  # noqa: ARG002
        form_data: dict[str, Any],
    ) -> FormHandlerResult:
        nonce = form_data.get("nonce", "")
        if nonce:
            from src.service.bots.form_handlers.nonce import validate_and_consume_nonce

            validate_and_consume_nonce(self._nonce_prefix, nonce)
        return FormHandlerResult(success=True, message=self._message)


__all__ = [
    "BaseFormHandler",
    "CancelFormHandler",
    "FormHandlerResult",
    "NonceCancelFormHandler",
]
