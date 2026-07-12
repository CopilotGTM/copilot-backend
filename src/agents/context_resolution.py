"""Deterministic pre-agent context resolution for common shorthand references."""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from src.agents.context import AgentContext
from src.agents.tools.intelligence.list_meetings_unified import list_meetings

logger = logging.getLogger(__name__)

ResolutionStatus = Literal["one_match", "multiple", "no_match", "unavailable"]

_MEETING_TERMS = re.compile(r"\b(meeting|meetings|call|calls|demo|demos)\b", re.I)
_CONTEXTUAL_MEETING_TERMS = re.compile(
    r"\b(upcoming|next|scheduled|last|previous|past|recent|recently|today|tomorrow)\b",
    re.I,
)
_NEW_MEETING_VERBS = re.compile(
    r"\b(schedule|create|set\s+up|book)\b.*\b(meeting|call|invite)\b|"
    r"\bsend\b.*\binvite\b",
    re.I,
)


def _meeting_reference_direction(text: str, metadata: dict[str, Any]) -> str | None:
    """Return the meeting direction when the user refers to an existing meeting."""
    if not text or not _MEETING_TERMS.search(text):
        return None
    if _NEW_MEETING_VERBS.search(text):
        return None

    metadata_direction = metadata.get("temporal_direction")
    if metadata_direction in {"past", "future", "present"}:
        return str(metadata_direction)

    if _CONTEXTUAL_MEETING_TERMS.search(text):
        lowered = text.lower()
        if any(term in lowered for term in ("upcoming", "next", "scheduled", "tomorrow")):
            return "future"
        if any(term in lowered for term in ("last", "previous", "past", "recent", "recently")):
            return "past"
        if "today" in lowered:
            return "present"

    return None


def _format_meeting_candidate(meeting: dict[str, Any]) -> dict[str, Any]:
    """Keep resolver metadata compact and safe for prompt injection."""
    deal = meeting.get("deal") or {}
    return {
        "id": meeting.get("event_id"),
        "title": meeting.get("title"),
        "start_at": meeting.get("start_at"),
        "end_at": meeting.get("end_at"),
        "organiser_email": meeting.get("organiser_email"),
        "attendee_count": meeting.get("attendee_count"),
        "has_transcript": meeting.get("has_transcript"),
        "has_summary": meeting.get("has_summary"),
        "has_prep": meeting.get("has_prep"),
        "deal": (
            {
                "deal_id": deal.get("deal_id"),
                "name": deal.get("name"),
                "company_name": deal.get("company_name"),
            }
            if deal
            else None
        ),
    }


def _build_resolution(
    *,
    status: ResolutionStatus,
    object_type: str,
    reference: str,
    checked: list[str],
    candidates: list[dict[str, Any]] | None = None,
    evidence_gap: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "object_type": object_type,
        "reference": reference,
        "checked": checked,
        "candidates": candidates or [],
        **({"evidence_gap": evidence_gap} if evidence_gap else {}),
    }


async def resolve_context_references(
    *,
    context: AgentContext,
    latest_user_text: str,
) -> dict[str, Any] | None:
    """Resolve high-confidence contextual references before the LLM can ask.

    The first hotfix target is meeting shorthand because the product already has
    a safe current-user calendar tool and the incident proved the model skipped it.
    """
    direction = _meeting_reference_direction(latest_user_text, context.metadata or {})
    if not direction:
        return None

    if not context.user_id:
        return _build_resolution(
            status="unavailable",
            object_type="meeting",
            reference=latest_user_text,
            checked=[],
            evidence_gap="current user calendar lookup is unavailable because user_id is missing",
        )

    try:
        meetings = await list_meetings(
            context=context,
            limit=5,
            time_direction=direction,  # type: ignore[arg-type]
        )
    except Exception as exc:  # pragma: no cover - defensive; tool should swallow DB errors
        logger.exception("context resolver meeting lookup failed: %s", exc)
        return _build_resolution(
            status="unavailable",
            object_type="meeting",
            reference=latest_user_text,
            checked=["calendar meetings"],
            evidence_gap="calendar lookup failed",
        )

    candidates = [_format_meeting_candidate(meeting) for meeting in meetings[:5]]
    if not candidates:
        return _build_resolution(
            status="no_match",
            object_type="meeting",
            reference=latest_user_text,
            checked=["calendar meetings"],
        )

    return _build_resolution(
        status="one_match" if len(candidates) == 1 else "multiple",
        object_type="meeting",
        reference=latest_user_text,
        checked=["calendar meetings"],
        candidates=candidates,
    )


__all__ = ["resolve_context_references"]
