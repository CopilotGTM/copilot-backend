"""Agent tools for updating, dismissing, and resolving existing risks/blockers.

These tools allow the Risk/Blocker Extraction Agent to manage the lifecycle
of existing items — updating assessments based on new evidence, dismissing
items that are no longer relevant, and resolving items that have been addressed.

New risks/blockers are created via the agent's structured output, not via tools.

The agent can mutate any item in the deal — both AI-flagged and user-flagged.
Each mutation is recorded in the audit trail and attributed to the automated
pipeline sentinel (AUTOMATED_PIPELINE_USER_ID); the original is_ai_generated
flag on the row is left untouched, so the provenance of the original flag is
preserved.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access
- deal_id: Scoped to the current deal context
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    RiskBlockerSeverity,
    RiskBlockerStatus,
    RiskBlockerType,
)
from src.service.crm.risks.contracts import (
    RiskBlockerActor,
    RiskBlockerNotFoundError,
    RiskBlockerWriteContext,
)
from src.service.crm.risks.lifecycle_service import RiskBlockerLifecycleService
from src.service.crm.risks.write_service import RiskBlockerWriteService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

# Sentinel UUID used to attribute system-driven actions to the automated pipeline
# (project-wide convention; matches deal_memory_service.py and the extraction service).
AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
AI_EXTRACTION_ACTOR_NAME = "AI Extraction Agent"


def _tool_context() -> RiskBlockerWriteContext:
    return RiskBlockerWriteContext(
        source="ai_extraction_tool",
        actor=RiskBlockerActor(
            actor_type="system",
            actor_id=AUTOMATED_PIPELINE_USER_ID,
            actor_name=AI_EXTRACTION_ACTOR_NAME,
        ),
    )


def _status_value(status: RiskBlockerStatus | str) -> str:
    return status.value if hasattr(status, "value") else str(status)


# ---------------------------------------------------------------------------
# Implementation functions
# ---------------------------------------------------------------------------


async def update_risk_blocker_impl(
    *,
    context: AgentContext,
    risk_blocker_id: str,
    item_type: str | None = None,
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

    async with context.acquire_session() as session:
        updates: dict[str, Any] = {}
        if item_type:
            with suppress(ValueError):
                updates["item_type"] = RiskBlockerType(item_type)

        if severity:
            with suppress(ValueError):
                updates["severity"] = RiskBlockerSeverity(severity)

        if description:
            updates["description"] = description

        if reasoning:
            updates["ai_reasoning"] = reasoning

        if recommended_next_step:
            updates["recommended_next_step"] = recommended_next_step

        service = RiskBlockerWriteService(session, organisation_id)
        item = await service.update_fields(rb_uuid, updates, _tool_context())
        if not item:
            return {"status": "not_found", "message": f"Risk/blocker {risk_blocker_id} not found"}

        if not service.last_changes:
            return {"status": "no_changes", "message": "No fields changed"}

        changes = service.last_changes
        await session.commit()

        logger.info("AI updated risk/blocker %s: %s", risk_blocker_id, list(changes.keys()))

        return {
            "status": "updated",
            "risk_blocker_id": risk_blocker_id,
            "deal_id": str(context.deal_id),
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

    async with context.acquire_session() as session:
        service = RiskBlockerLifecycleService(session, organisation_id)
        current_status_raw = await service.get_status(rb_uuid)
        if current_status_raw is None:
            return {"status": "not_found", "message": f"Risk/blocker {risk_blocker_id} not found"}

        current_status = _status_value(current_status_raw)
        if current_status in [RiskBlockerStatus.RESOLVED.value, RiskBlockerStatus.DISMISSED.value]:
            return {
                "status": "skipped",
                "message": f"Already {current_status} — no action needed",
            }

        previous_status = current_status
        try:
            await service.dismiss(rb_uuid, context=_tool_context(), reason=reason)
        except RiskBlockerNotFoundError:
            return {"status": "not_found", "message": f"Risk/blocker {risk_blocker_id} not found"}
        await session.commit()

        logger.info("AI dismissed risk/blocker %s: %s", risk_blocker_id, reason[:100])

        return {
            "status": "dismissed",
            "risk_blocker_id": risk_blocker_id,
            "deal_id": str(context.deal_id),
            "previous_status": previous_status,
            "new_status": RiskBlockerStatus.DISMISSED.value,
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

    async with context.acquire_session() as session:
        service = RiskBlockerLifecycleService(session, organisation_id)
        current_status_raw = await service.get_status(rb_uuid)
        if current_status_raw is None:
            return {"status": "not_found", "message": f"Risk/blocker {risk_blocker_id} not found"}

        current_status = _status_value(current_status_raw)
        if current_status in [RiskBlockerStatus.RESOLVED.value, RiskBlockerStatus.DISMISSED.value]:
            return {
                "status": "skipped",
                "message": f"Already {current_status} — no action needed",
            }

        previous_status = current_status
        try:
            await service.resolve(rb_uuid, resolution_notes, context=_tool_context())
        except RiskBlockerNotFoundError:
            return {"status": "not_found", "message": f"Risk/blocker {risk_blocker_id} not found"}
        await session.commit()

        logger.info("AI resolved risk/blocker %s: %s", risk_blocker_id, resolution_notes[:100])

        return {
            "status": "resolved",
            "risk_blocker_id": risk_blocker_id,
            "deal_id": str(context.deal_id),
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
        item_type: str | None = None,
        severity: str | None = None,
        description: str | None = None,
        reasoning: str | None = None,
        recommended_next_step: str | None = None,
    ) -> dict[str, Any]:
        """Update an existing risk or blocker when new evidence changes the assessment.

        This tool updates classification, content, and severity — it does NOT change the status.
        To change status, use dismiss_risk_blocker or resolve_risk_blocker instead.

        When to use: a risk/blocker already exists but new deal evidence changes
        whether it should be a risk vs blocker, how severe it is, what the
        description should say, or what the next step is.
        Example: a stalled legal review that was previously tracked as a risk
        should be updated to BLOCKER once the buyer says the deal cannot proceed
        until legal signs off.

        Args:
            risk_blocker_id: UUID of the item to update (from get_existing_risks_blockers)
            item_type: New classification if changed: RISK | BLOCKER
            severity: New severity level if changed: CRITICAL | HIGH | MEDIUM | LOW
            description: Updated description incorporating new evidence
            reasoning: Updated reasoning explaining the changed assessment
            recommended_next_step: Updated action — who does what, by when, and why
        """
        logger.info("[TOOL CALLED] update_risk_blocker - id=%s", risk_blocker_id)

        result = cast(
            dict[str, Any],
            await execute_with_capabilities(
                capabilities_getter(),
                tool_name="UpdateRiskBlocker",
                tool_func=update_risk_blocker_impl,
                parameters={
                    "context": context,
                    "risk_blocker_id": risk_blocker_id,
                    "item_type": item_type,
                    "severity": severity,
                    "description": description,
                    "reasoning": reasoning,
                    "recommended_next_step": recommended_next_step,
                },
                description="Updating risk or blocker",
                fallback_value={"status": "error", "message": "Tool execution failed"},
            ),
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
        """Dismiss a risk/blocker that was NEVER a real issue or is NO LONGER applicable.

        Works on both AI-flagged and user-flagged items. The dismissal is recorded
        in the audit trail attributed to the AI extraction agent, so the human
        provenance of the original flag is preserved.

        Dismiss means "this should not have been flagged" or "circumstances changed and it no longer applies."
        If the issue WAS real but got fixed, use resolve_risk_blocker instead.

        Args:
            risk_blocker_id: UUID of the item to dismiss (from get_existing_risks_blockers)
            reason: Why this is being dismissed — cite the evidence. Example: "Prospect dropped competitor evaluation per email on Feb 3"
        """
        logger.info("[TOOL CALLED] dismiss_risk_blocker - id=%s", risk_blocker_id)

        result = cast(
            dict[str, Any],
            await execute_with_capabilities(
                capabilities_getter(),
                tool_name="DismissRiskBlocker",
                tool_func=dismiss_risk_blocker_impl,
                parameters={
                    "context": context,
                    "risk_blocker_id": risk_blocker_id,
                    "reason": reason,
                },
                description="Dismissing risk or blocker",
                fallback_value={"status": "error", "message": "Tool execution failed"},
            ),
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

        result = cast(
            dict[str, Any],
            await execute_with_capabilities(
                capabilities_getter(),
                tool_name="ResolveRiskBlocker",
                tool_func=resolve_risk_blocker_impl,
                parameters={
                    "context": context,
                    "risk_blocker_id": risk_blocker_id,
                    "resolution_notes": resolution_notes,
                },
                description="Resolving risk or blocker",
                fallback_value={"status": "error", "message": "Tool execution failed"},
            ),
        )

        logger.info("[TOOL RESULT] resolve_risk_blocker - %s", result.get("status", "unknown"))
        return result

    return resolve_risk_blocker_tool


__all__ = [
    "create_update_risk_blocker",
    "create_dismiss_risk_blocker",
    "create_resolve_risk_blocker",
]
