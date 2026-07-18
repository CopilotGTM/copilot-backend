"""Agent tool for updating safe scalar fields on an explicitly resolved deal.

Conversational callers resolve a deal with ``search_deals`` and pass its ID;
autonomous callers pass the ID from their deal-scoped workflow. The immutable
organization context remains the tenant boundary for every write.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.agents.tools.crm.access import AgentCRMAccessError, resolve_agent_access_actor
from src.contracts.crm.deal_write import DealUpdateRequest
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.service.crm.deals.contracts import DealWriteServiceError
from src.service.crm.deals.write_service import DealWriteService
from src.service.crm.internal.deal_update_policy import classify_deal_update_policy

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
ALLOWED_DEAL_FIELDS = {
    "name",
    "amount",
    "currency",
    "source_created_at",
    "description",
    "expected_close_date",
    "priority",
    "probability",
    "forecast_category",
    "lead_source",
    "deal_type",
}


class DealFieldUpdateInput(BaseModel):
    """LLM-facing scalar fields accepted by the deal field update action."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=500)
    amount: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    source_created_at: datetime | None = None
    description: str | None = None
    expected_close_date: datetime | None = None
    priority: str | None = None
    probability: float | None = Field(default=None, ge=0, le=100)
    forecast_category: str | None = None
    lead_source: str | None = None
    deal_type: str | None = None


def _resolve_deal_id(context: AgentContext, deal_id: str) -> UUID:
    """Validate a caller-provided deal ID against any scoped workflow boundary.

    General chat has no scoped deal and may target any deal resolved inside the
    organization. Autonomous deal workflows already have an immutable target,
    so their explicit argument must match it exactly.
    """
    try:
        resolved_deal_id = UUID(deal_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid deal_id. Search for the deal again.") from exc
    if context.deal_id and resolved_deal_id != context.deal_id:
        raise ValueError("deal_id does not match the scoped deal context.")
    return resolved_deal_id


def _result(
    status: str,
    reason: str,
    *,
    applied_fields: dict[str, Any] | None = None,
    approval_payload: dict[str, Any] | None = None,
    deal_id: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "action": "update_deal_fields",
        "reason": reason,
        "applied_fields": applied_fields or {},
        "requires_approval": status == "approval_required",
        "approval_payload": approval_payload,
    }
    if deal_id:
        result["deal_id"] = deal_id
    return result


def _deal_context_fields(deal: Any) -> dict[str, Any]:
    context: dict[str, Any] = {}
    if getattr(deal, "amount", None) is not None:
        context["amount"] = float(deal.amount)
    if getattr(deal, "predicted_amount", None) is not None:
        context["predicted_amount"] = float(deal.predicted_amount)
    for key in ("currency", "deal_status", "stage_name", "pipeline_name"):
        value = getattr(deal, key, None)
        if value is not None:
            context[key] = value
    if getattr(deal, "stage_id", None):
        context["stage_id"] = str(deal.stage_id)
    if getattr(deal, "pipeline_id", None):
        context["pipeline_id"] = str(deal.pipeline_id)
    return context


def _normalize_deal_fields(
    fields: DealFieldUpdateInput | dict[str, Any],
) -> dict[str, Any]:
    payload = fields if isinstance(fields, dict) else fields.model_dump(exclude_unset=True)
    request = DealUpdateRequest(**payload)
    return request.model_dump(exclude_unset=True)


def create_update_deal_fields(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Build the organization-scoped `update_deal_fields` tool.

    Callers must pass a deal ID returned by ``search_deals`` or creation. The
    tool validates that identifier before applying field and approval policy.
    """

    async def update_deal_fields_tool(
        deal_id: str,
        fields: DealFieldUpdateInput,
        directly_requested: bool = False,
        source: str = "agent",
    ) -> dict[str, Any]:
        """Update safe scalar fields on one resolved deal.

        In general chat, first use ``search_deals`` and disambiguate multiple
        results. Deal-scoped workflows pass their assigned deal ID. Supported
        field keys are name, amount, currency, source_created_at, description,
        expected_close_date, priority, probability, forecast_category,
        lead_source, and deal_type.

        Set ``directly_requested=True`` and ``source="chat"`` when the current
        user explicitly requested the change. Leave the defaults only for
        inferred or autonomous updates that require approval.
        """
        _ = capabilities_getter
        if not context.organisation_id:
            return _result("failed", "Organisation context is required.")
        try:
            resolved_deal_id = _resolve_deal_id(context, deal_id)
        except ValueError as exc:
            return _result("failed", str(exc))
        if isinstance(fields, dict):
            unsupported = sorted(set(fields) - ALLOWED_DEAL_FIELDS)
            if unsupported:
                return _result("skipped", f"Fields are not supported: {', '.join(unsupported)}")
        try:
            normalized_fields = _normalize_deal_fields(fields)
        except ValidationError as exc:
            errors = exc.errors()
            reason = str(errors[0]["msg"]) if errors else "Invalid deal field payload."
            return _result("failed", reason)
        if not normalized_fields:
            return _result("failed", "At least one field is required.")

        unsupported = sorted(set(normalized_fields) - ALLOWED_DEAL_FIELDS)
        if unsupported:
            return _result("skipped", f"Fields are not supported: {', '.join(unsupported)}")

        effective_fields = (
            normalized_fields
            if directly_requested
            else {field: value for field, value in normalized_fields.items() if value is not None}
        )
        if not effective_fields:
            return _result("skipped", "No new value was provided for the update.")

        approval_fields: dict[str, Any] = {}
        # Evaluate all fields first so the tool never commits a partial field set.
        for field, new_value in effective_fields.items():
            if new_value is None:
                continue
            decision = classify_deal_update_policy(
                action="update_deal_fields",
                field=field,
                old_value=None,
                new_value=new_value,
                source=source,
                directly_requested=directly_requested,
            )
            if decision.status == "skipped":
                return _result("skipped", decision.reason)
            if decision.requires_approval:
                approval_fields[field] = new_value

        if approval_fields:
            # Return a single approval payload for the sensitive subset, keeping
            # the safe updates unchanged for clearer operator review.
            return _result(
                "approval_required",
                "One or more deal field updates require approval.",
                approval_payload={
                    "deal_id": str(resolved_deal_id),
                    "fields": approval_fields,
                    "source": source,
                },
            )

        actor_user_id = context.user_id or AUTOMATED_PIPELINE_USER_ID
        try:
            access_actor = await resolve_agent_access_actor(context)
        except AgentCRMAccessError as exc:
            return _result("failed", str(exc))
        # Keep the write transaction narrow and explicit: build service once then
        # commit only after the mutate call succeeds.
        async for master_session in acquire_master_async_session():
            async for org_session in acquire_org_async_session(context.organisation_id):
                service = DealWriteService(
                    org_session=org_session,
                    master_session=master_session,
                    organisation_id=context.organisation_id,
                    actor_user_id=actor_user_id,
                    access_actor=access_actor,
                )
                try:
                    deal = await service.update_deal(resolved_deal_id, effective_fields)
                    await org_session.commit()
                    return _result(
                        "applied",
                        "Deal fields updated.",
                        applied_fields=effective_fields,
                        deal_id=str(deal.deal_id),
                    ) | _deal_context_fields(deal)
                except DealWriteServiceError as exc:
                    await org_session.rollback()
                    return _result("failed", exc.message)
                except Exception:
                    await org_session.rollback()
                    return _result("failed", "Unexpected error while updating deal fields.")

        return _result("failed", "Could not acquire database session.")

    return update_deal_fields_tool
