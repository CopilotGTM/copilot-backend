"""Widget builders for the application-provided Sales Dashboard."""

from .deal_tables import build_deal_table_widgets
from .diagnostics import build_diagnostic_widgets
from .segmentation import build_segmentation_widgets
from .stage_movement import build_stage_movement_widgets
from .summary_cards import build_summary_card_widgets

__all__ = [
    "build_deal_table_widgets",
    "build_diagnostic_widgets",
    "build_segmentation_widgets",
    "build_stage_movement_widgets",
    "build_summary_card_widgets",
]
