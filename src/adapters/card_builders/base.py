"""Channel-agnostic form definitions for interactive card builders.

This module provides declarative form definitions that can be rendered
into channel-specific payloads (Teams Adaptive Cards, Slack Block Kit, etc.).

Architecture:
- FormField: Single form input definition
- FormDefinition: Complete form with fields, actions, and metadata
- FormCardBuilder: Abstract base class for channel-specific form renderers
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FormField:
    """Declarative form field definition (channel-agnostic).

    Attributes:
        id: Unique field identifier (used as key in submitted form data)
        label: Human-readable label displayed to the user
        field_type: Input type - "text", "number", "date", "choice", "toggle"
        required: Whether the field must be filled before submission
        value: Pre-filled default value
        placeholder: Hint text shown when field is empty
        choices: For "choice" type: list of {"title": ..., "value": ...}
        multiline: For "text" type: allow multi-line input
        min_val: For "number" type: minimum allowed value
        max_val: For "number" type: maximum allowed value
    """

    id: str
    label: str
    field_type: str = "text"
    required: bool = False
    value: Any = None
    placeholder: str = ""
    choices: list[dict[str, str]] = field(default_factory=list)
    multiline: bool = False
    min_val: float | None = None
    max_val: float | None = None


@dataclass
class FormDefinition:
    """Channel-agnostic form definition.

    Describes a complete interactive form that can be rendered
    by any FormCardBuilder into a channel-specific payload.

    Attributes:
        title: Form title displayed prominently
        subtitle: Optional subtitle or description
        fields: List of form fields
        submit_action: Action identifier sent on submit (e.g. "create_deal_submit")
        submit_label: Text on the submit button
        cancel_action: Optional action identifier sent on cancel
        cancel_label: Text on the cancel button
        metadata: Additional data to include with the submission (e.g. org_id)
    """

    title: str
    subtitle: str | None = None
    fields: list[FormField] = field(default_factory=list)
    submit_action: str = "form_submit"
    submit_label: str = "Submit"
    cancel_action: str | None = None
    cancel_label: str = "Cancel"
    metadata: dict[str, str] = field(default_factory=dict)


class FormCardBuilder(ABC):
    """Abstract builder that converts FormDefinition into a channel payload dict.

    Each channel (Teams, Slack, etc.) implements this to produce
    its native card format from a declarative FormDefinition.
    """

    @abstractmethod
    def build(self, form: FormDefinition) -> dict[str, Any]:
        """Build a channel-specific card payload from a FormDefinition.

        Args:
            form: Channel-agnostic form definition

        Returns:
            Channel-specific payload dict ready to send via messaging API
        """
        ...


__all__ = [
    "FormField",
    "FormDefinition",
    "FormCardBuilder",
]
