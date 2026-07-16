"""Update internal deal notes from GeneralAgent chat workflows.

The caller must resolve both the deal and note first. Organisation identity
comes only from ``AgentContext``; the write service then enforces deal ownership
and rejects provider-owned notes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.agents.tools.base import execute_with_capabilities
from src.service.crm.notes.contracts import NotesServiceError
from src.service.crm.notes.write_service import NotesWriteService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def update_deal_note(
    *,
    context: AgentContext,
    deal_id: str,
    note_id: str,
    body: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Update explicitly supplied fields on one resolved internal deal note.

    Args:
        context: Immutable organisation and user context for this chat.
        deal_id: Resolved UUID of the note's deal.
        note_id: Resolved UUID returned by the note listing tool.
        body: Replacement note text, when requested.
        title: Replacement note title, when requested.

    Returns:
        A structured update result or a user-safe error.
    """
    try:
        parsed_deal_id = UUID(deal_id)
        parsed_note_id = UUID(note_id)
    except ValueError:
        return {"status": "error", "message": "Invalid deal_id or note_id."}

    payload: dict[str, Any] = {}
    if body is not None:
        payload["content"] = body.strip()
    if title is not None:
        payload["subject"] = title.strip()
    if not payload:
        return {"status": "error", "message": "Provide a note title or body to update."}

    async with context.acquire_session() as session:
        service = NotesWriteService(session, context.organisation_id)
        try:
            note = await service.update_user_note(
                deal_id=parsed_deal_id,
                note_id=parsed_note_id,
                payload=payload,
            )
            await session.commit()
        except NotesServiceError as exc:
            await session.rollback()
            return {"status": "error", "message": exc.message}
        except Exception:
            await session.rollback()
            logger.exception("update_deal_note: unexpected error")
            return {"status": "error", "message": "Could not update the note."}

    label = note.title or "Untitled note"
    return {
        "status": "updated",
        "note_id": str(note.note_id),
        "deal_id": str(parsed_deal_id),
        "title": note.title,
        "body": note.body,
        "message": (
            f"Updated note **{label}**. Note ID: {note.note_id}. Deal ID: {parsed_deal_id}."
        ),
    }


def create_update_deal_note(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Bind the org-scoped deal-note update contract to one agent context."""

    async def update_deal_note_tool(
        deal_id: str,
        note_id: str,
        body: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Update a resolved internal CRM note without guessing record IDs.

        First resolve the deal with ``search_deals`` and the note with
        ``list_deal_notes``. Send only fields the user explicitly asked to change.
        External CRM notes are read-only and will be rejected by the write service.
        """
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="UpdateDealNote",
            tool_func=update_deal_note,
            parameters={
                "context": context,
                "deal_id": deal_id,
                "note_id": note_id,
                "body": body,
                "title": title,
            },
            description="Updating deal note",
            fallback_value={"status": "error", "message": "Could not update the note."},
        )
        return dict(result)

    return update_deal_note_tool


__all__ = ["create_update_deal_note", "update_deal_note"]
