"""Meeting intelligence agent tools.

Tools for listing and retrieving meeting information for deals.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agents.tools.intelligence.get_compressed_transcript import (
        create_get_compressed_transcript,
    )
    from src.agents.tools.intelligence.get_meeting_deal_context import (
        create_get_meeting_deal_context,
    )
    from src.agents.tools.intelligence.get_meeting_details import create_get_meeting_details
    from src.agents.tools.intelligence.get_meeting_summary import create_get_meeting_summary
    from src.agents.tools.intelligence.get_meeting_transcript import create_get_meeting_transcript
    from src.agents.tools.intelligence.list_meetings import create_list_deal_meetings
    from src.agents.tools.intelligence.list_meetings_by_date import create_list_meetings_by_date

_EXPORTS = {
    "create_list_deal_meetings": (
        "src.agents.tools.intelligence.list_meetings",
        "create_list_deal_meetings",
    ),
    "create_get_meeting_details": (
        "src.agents.tools.intelligence.get_meeting_details",
        "create_get_meeting_details",
    ),
    "create_get_meeting_summary": (
        "src.agents.tools.intelligence.get_meeting_summary",
        "create_get_meeting_summary",
    ),
    "create_get_meeting_deal_context": (
        "src.agents.tools.intelligence.get_meeting_deal_context",
        "create_get_meeting_deal_context",
    ),
    "create_get_meeting_transcript": (
        "src.agents.tools.intelligence.get_meeting_transcript",
        "create_get_meeting_transcript",
    ),
    "create_get_compressed_transcript": (
        "src.agents.tools.intelligence.get_compressed_transcript",
        "create_get_compressed_transcript",
    ),
    "create_list_meetings_by_date": (
        "src.agents.tools.intelligence.list_meetings_by_date",
        "create_list_meetings_by_date",
    ),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, export_name = _EXPORTS[name]
    module = import_module(module_name)
    return getattr(module, export_name)
