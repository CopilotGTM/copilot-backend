"""Agent tools for updating, dismissing, and resolving existing risks/blockers.

These tools allow the Risk/Blocker Extraction Agent to manage the lifecycle
of existing items — updating assessments based on new evidence, dismissing
items that are no longer relevant, and resolving items that have been addressed.

New risks/blockers are created via the agent's structured output, not via tools.

All mutations are marked as AI-generated with full audit trails.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access
- deal_id: Scoped to the current deal context
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    DealRiskBlocker,
    RiskBlockerSeverity,
    RiskBlockerStatus,
)
from src.domain.models.organisation_tables.sync.timeline_models import TimelineEventType
from src.service.crm.timeline.deal_timeline_sync_service import (
    build_risk_blocker_timeline_event,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Implementation functions
# ---------------------------------------------------------------------------


async def update_risk_blocker_impl(
    *,
    context: AgentContext,
    risk_blocker_id: str,
    severity: str | None = None,
    description: str | None = None,
    reasoning: str | None = None,
    recommended_next_step: str | None = None,
) -> dict[str, Any]:
    """Update an existing risk/blocker with new evidence or changed assessment.

    SECURITY: organisation_id from context (immutable). Verifies item belongs to org.
    """
    organisation_id = context.organisation_id
    if not organisation_id:
        return {"status": "error", "message": "Missing organisation context"}

    try:
        rb_uuid = UUID(risk_blocker_id)
    except ValueError:
        return {"status": "error", "message": f"Invalid risk_blocker_id: {risk_blocker_id}"}

    c = cast(Any, DealRiskBlocker).__table__.c

    async with context.acquire_session() as session:
        result = await session.execute(
            select(DealRiskBlocker).where(
                c.risk_blocker_id == rb_uuid,
                c.organisation_id == organisation_id,
            )
        )
        item = result.scalar_one_or_none()

        if not item:
            return {"status": "not_found", "message": f"Risk/blocker {risk_blocker_id} not found"}

        changes: dict[str, dict[str, str]] = {}

        if severity:
            try:
                new_severity = RiskBlockerSeverity(severity)
                old_val = (
                    item.severity.value if hasattr(item.severity, "value") else str(item.severity)
                )
                if old_val != new_severity.value:
                    changes["severity"] = {"old": old_val, "new": new_severity.value}
                    item.severity = new_severity
            except ValueError:
                pass

        if description:
            old_desc = item.description or ""
            if old_desc != description:
                changes["description"] = {
                    "old": old_desc[:100] + "..." if len(old_desc) > 100 else old_desc,
                    "new": description[:100] + "..." if len(description) > 100 else description,
                }
                item.description = description

        if reasoning:
            item.ai_reasoning = reasoning

        if recommended_next_step:
            data = item.risk_blocker_data or {}
            data["recommended_next_step"] = recommended_next_step
            item.risk_blocker_data = data

        if not changes and not reasoning and not recommended_next_step:
            return {"status": "no_changes", "message": "No fields changed"}

        # Audit trail
        data = item.risk_blocker_data or {}
        trail = data.get("audit_trail", [])
        trail.append(
            {
                "action": "ai_updated",
                "timestamp": datetime.now(UTC).isoformat(),
                "actor_type": "system",
                "actor_id": "risk_blocker_extraction_agent",
                "details": {"fields_changed": list(changes.keys())}
                if changes
                else {"note": "reasoning/next_step updated"},
            }
        )
        data["audit_trail"] = trail
        item.risk_blocker_data = data

        item.updated_at = datetime.now(UTC)

        await session.commit()

        logger.info("AI updated risk/blocker %s: %s", risk_blocker_id, list(changes.keys()))

        return {
            "status": "updated",
            "risk_blocker_id": risk_blocker_id,
            "changes": changes,
        }


async def dismiss_risk_blocker_impl(
    *,
    context: AgentContext,
    risk_blocker_id: str,
    reason: str,
) -> dict[str, Any]:
    """Dismiss a risk/blocker that is no longer relevant.

    SECURITY: organisation_id from context (immutable). Verifies item belongs to org.
    """
    organisation_id = context.organisation_id
    if not organisation_id:
        return {"status": "error", "message": "Missing organisation context"}

    try:
        rb_uuid = UUID(risk_blocker_id)
    except ValueError:
        return {"status": "error", "message": f"Invalid risk_blocker_id: {risk_blocker_id}"}

    c = cast(Any, DealRiskBlocker).__table__.c

    async with context.acquire_session() as session:
        result = await session.execute(
            select(DealRiskBlocker).where(
                c.risk_blocker_id == rb_uuid,
                c.organisation_id == organisation_id,
            )
        )
        item = result.scalar_one_or_none()

        if not item:
            return {"status": "not_found", "message": f"Risk/blocker {risk_blocker_id} not found"}

        current_status = item.status.value if hasattr(item.status, "value") else str(item.status)

        if current_status in [RiskBlockerStatus.RESOLVED.value, RiskBlockerStatus.DISMISSED.value]:
            return {
                "status": "skipped",
                "message": f"Already {current_status} — no action needed",
            }
        if current_status == RiskBlockerStatus.OPEN.value:
            return {
                "status": "skipped",
                "message": "User-confirmed item (OPEN) — AI cannot dismiss. Only AI_SUGGESTED items can be AI-dismissed.",
            }

        previous_status = current_status
        item.status = RiskBlockerStatus.AI_DISMISSED
        item.updated_at = datetime.now(UTC)

        # Audit trail
        data = item.risk_blocker_data or {}
        trail = data.get("audit_trail", [])
        trail.append(
            {
                "action": "ai_dismissed",
                "timestamp": datetime.now(UTC).isoformat(),
                "actor_type": "system",
                "actor_id": "risk_blocker_extraction_agent",
                "details": {
                    "previous_status": previous_status,
                    "reason": reason,
                },
            }
        )
        data["audit_trail"] = trail
        item.risk_blocker_data = data

        await session.commit()

        logger.info("AI dismissed risk/blocker %s: %s", risk_blocker_id, reason[:100])

        return {
            "status": "dismissed",
            "risk_blocker_id": risk_blocker_id,
            "previous_status": previous_status,
            "reason": reason,
        }


async def resolve_risk_blocker_impl(
    *,
    context: AgentContext,
    risk_blocker_id: str,
    resolution_notes: str,
) -> dict[str, Any]:
    """Mark a risk/blocker as resolved based on deal evidence.

    SECURITY: organisation_id from context (immutable). Verifies item belongs to org.
    """
    organisation_id = context.organisation_id
    if not organisation_id:
        return {"status": "error", "message": "Missing organisation context"}

    try:
        rb_uuid = UUID(risk_blocker_id)
    except ValueError:
        return {"status": "error", "message": f"Invalid risk_blocker_id: {risk_blocker_id}"}

    c = cast(Any, DealRiskBlocker).__table__.c

    async with context.acquire_session() as session:
        result = await session.execute(
            select(DealRiskBlocker).where(
                c.risk_blocker_id == rb_uuid,
                c.organisation_id == organisation_id,
            )
        )
        item = result.scalar_one_or_none()

        if not item:
            return {"status": "not_found", "message": f"Risk/blocker {risk_blocker_id} not found"}

        current_status = item.status.value if hasattr(item.status, "value") else str(item.status)

        if current_status in [RiskBlockerStatus.RESOLVED.value, RiskBlockerStatus.DISMISSED.value]:
            return {
                "status": "skipped",
                "message": f"Already {current_status} — no action needed",
            }

        previous_status = current_status
        item.status = RiskBlockerStatus.RESOLVED
        item.resolution_notes = resolution_notes
        item.resolved_at = datetime.now(UTC)
        item.resolved_by = "AI Risk/Blocker Agent"
        item.updated_at = datetime.now(UTC)

        # Audit trail
        data = item.risk_blocker_data or {}
        trail = data.get("audit_trail", [])
        trail.append(
            {
                "action": "ai_resolved",
                "timestamp": datetime.now(UTC).isoformat(),
                "actor_type": "system",
                "actor_id": "risk_blocker_extraction_agent",
                "details": {
                    "previous_status": previous_status,
                    "resolution_notes": resolution_notes,
                },
            }
        )
        data["audit_trail"] = trail
        item.risk_blocker_data = data

        # Timeline event
        timeline_event = build_risk_blocker_timeline_event(
            organisation_id=organisation_id,
            deal_id=item.deal_id,
            risk_blocker=item,
            event_type=TimelineEventType.BLOCKER_RESOLVED,
        )
        session.add(timeline_event)

        await session.commit()

        logger.info("AI resolved risk/blocker %s: %s", risk_blocker_id, resolution_notes[:100])

        return {
            "status": "resolved",
            "risk_blocker_id": risk_blocker_id,
            "previous_status": previous_status,
            "resolution_notes": resolution_notes,
        }


# ---------------------------------------------------------------------------
# Factory functions (bound to context for agent use)
# ---------------------------------------------------------------------------


def create_update_risk_blocker(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory for the update_risk_blocker tool."""

    async def update_risk_blocker_tool(
        risk_blocker_id: str,
        severity: str | None = None,
        description: str | None = None,
        reasoning: str | None = None,
        recommended_next_step: str | None = None,
    ) -> dict[str, Any]:
        """Update an existing risk or blocker when new evidence changes the assessment.

        This tool updates content and severity — it does NOT change the status.
        To change status, use dismiss_risk_blocker or resolve_risk_blocker instead.

        When to use: a risk/blocker already exists but new deal evidence changes
        how severe it is, what the description should say, or what the next step is.
        Example: a MEDIUM budget risk escalates to HIGH after CFO said "cutting all discretionary spend."

        Args:
            risk_blocker_id: UUID of the item to update (from get_existing_risks_blockers)
            severity: New severity level if changed: CRITICAL | HIGH | MEDIUM | LOW
            description: Updated description incorporating new evidence
            reasoning: Updated reasoning explaining the changed assessment
            recommended_next_step: Updated action — who does what, by when, and why
        """
        logger.info("[TOOL CALLED] update_risk_blocker - id=%s", risk_blocker_id)

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="UpdateRiskBlocker",
            tool_func=update_risk_blocker_impl,
            parameters={
                "context": context,
                "risk_blocker_id": risk_blocker_id,
                "severity": severity,
                "description": description,
                "reasoning": reasoning,
                "recommended_next_step": recommended_next_step,
            },
            description="Updating risk/blocker",
            fallback_value={"status": "error", "message": "Tool execution failed"},
        )

        logger.info("[TOOL RESULT] update_risk_blocker - %s", result.get("status", "unknown"))
        return result

    return update_risk_blocker_tool


def create_dismiss_risk_blocker(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory for the dismiss_risk_blocker tool."""

    async def dismiss_risk_blocker_tool(
        risk_blocker_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Dismiss an AI-suggested risk/blocker that was NEVER a real issue or is NO LONGER applicable.

        ONLY works on AI_SUGGESTED items. Cannot dismiss user-confirmed items (status=OPEN).

        Dismiss means "this should not have been flagged" or "circumstances changed and it no longer applies."
        If the issue WAS real but got fixed, use resolve_risk_blocker instead.

        Args:
            risk_blocker_id: UUID of the item to dismiss (from get_existing_risks_blockers)
            reason: Why this is being dismissed — cite the evidence. Example: "Prospect dropped competitor evaluation per email on Feb 3"
        """
        logger.info("[TOOL CALLED] dismiss_risk_blocker - id=%s", risk_blocker_id)

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="DismissRiskBlocker",
            tool_func=dismiss_risk_blocker_impl,
            parameters={
                "context": context,
                "risk_blocker_id": risk_blocker_id,
                "reason": reason,
            },
            description="Dismissing risk/blocker",
            fallback_value={"status": "error", "message": "Tool execution failed"},
        )

        logger.info("[TOOL RESULT] dismiss_risk_blocker - %s", result.get("status", "unknown"))
        return result

    return dismiss_risk_blocker_tool


def create_resolve_risk_blocker(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory for the resolve_risk_blocker tool."""

    async def resolve_risk_blocker_tool(
        risk_blocker_id: str,
        resolution_notes: str,
    ) -> dict[str, Any]:
        """Mark a risk/blocker as resolved — the issue WAS real and IS now fixed.

        If the issue was never real or no longer applies, use dismiss_risk_blocker instead.

        Resolve means the obstacle existed and has been explicitly addressed in the deal.
        Example: "CTO confirmed PostgreSQL connector works after Feb 10 technical review."

        Args:
            risk_blocker_id: UUID of the item to resolve (from get_existing_risks_blockers)
            resolution_notes: What resolved it, when, and who was involved. Example: "CFO approved $120K budget in Feb 15 meeting after ROI presentation"
        """
        logger.info("[TOOL CALLED] resolve_risk_blocker - id=%s", risk_blocker_id)

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ResolveRiskBlocker",
            tool_func=resolve_risk_blocker_impl,
            parameters={
                "context": context,
                "risk_blocker_id": risk_blocker_id,
                "resolution_notes": resolution_notes,
            },
            description="Resolving risk/blocker",
            fallback_value={"status": "error", "message": "Tool execution failed"},
        )

        logger.info("[TOOL RESULT] resolve_risk_blocker - %s", result.get("status", "unknown"))
        return result

    return resolve_risk_blocker_tool


__all__ = [
    "create_update_risk_blocker",
    "create_dismiss_risk_blocker",
    "create_resolve_risk_blocker",
]
