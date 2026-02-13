"""Channel-agnostic form definitions for interactive card builders.

This module provides declarative form definitions that can be rendered
into channel-specific payloads (Teams Adaptive Cards, Slack Block Kit, etc.).

Architecture:
- FormField: Single form input definition
- FormDefinition: Complete form with fields, actions, and metadata
- VisualizationPoint/Series/Definition: Declarative visualization summary cards
- FormCardBuilder: Abstract base class for channel-specific form renderers
- VisualizationCardBuilder: Abstract base class for visualization card renderers
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


@dataclass
class VisualizationPoint:
    """Single data point used in a visualization series."""

    label: str
    value: float | int | str


@dataclass
class VisualizationSeries:
    """Single named series for a chart."""

    name: str
    points: list[VisualizationPoint] = field(default_factory=list)


@dataclass
class VisualizationDefinition:
    """Channel-agnostic visualization summary card definition.

    This does not attempt to encode full chart rendering instructions for Teams.
    Instead, it carries a compact summary plus an optional link to a rich
    interactive view (dialog/tab/web app).
    """

    title: str
    chart_type: str
    subtitle: str | None = None
    insight_text: str | None = None
    series: list[VisualizationSeries] = field(default_factory=list)
    interactive_url: str | None = None
    open_label: str = "Open Interactive View"


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


class VisualizationCardBuilder(ABC):
    """Abstract builder that converts VisualizationDefinition into channel payload."""

    @abstractmethod
    def build_visualization(self, visualization: VisualizationDefinition) -> dict[str, Any]:
        """Build a channel-specific visualization summary payload."""
        ...


__all__ = [
    "FormField",
    "FormDefinition",
    "VisualizationPoint",
    "VisualizationSeries",
    "VisualizationDefinition",
    "FormCardBuilder",
    "VisualizationCardBuilder",
]
