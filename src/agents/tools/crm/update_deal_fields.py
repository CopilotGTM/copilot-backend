"""Agent tool for updating safe scalar CRM deal fields on a scoped context.

The function is used by conversational and autonomous deal-update flows.
The tool always validates tenant/deal context and applies approval policy
before it writes through the shared write service.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.service.crm.internal.deal_update_policy import classify_deal_update_policy
from src.service.crm.internal.deal_write_service import DealWriteService, DealWriteServiceError

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
ALLOWED_DEAL_FIELDS = {
    "amount",
    "currency",
    "description",
    "expected_close_date",
    "close_date",
    "priority",
    "probability",
    "forecast_category",
    "lead_source",
    "deal_type",
}


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


def create_update_deal_fields(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Build the `update_deal_fields` tool for the currently scoped deal.

    The returned callable enforces supported field constraints, routes
    sensitive updates through approval when inference-based, and writes only if
    the full update set is policy-compliant.
    """

    async def update_deal_fields_tool(
        fields: dict[str, Any],
        directly_requested: bool = False,
        source: str = "agent",
    ) -> dict[str, Any]:
        """Apply safe scalar field updates with policy-driven approval guards."""
        _ = capabilities_getter
        # Both org and deal are required boundary inputs; no trust in raw IDs.
        if not context.organisation_id or not context.deal_id:
            return _result("failed", "Deal context is required.")
        if not fields:
            return _result("failed", "At least one field is required.")

        unsupported = sorted(set(fields) - ALLOWED_DEAL_FIELDS)
        if unsupported:
            return _result("skipped", f"Fields are not supported: {', '.join(unsupported)}")

        approval_fields: dict[str, Any] = {}
        # Evaluate all fields first so the tool never commits a partial field set.
        for field, new_value in fields.items():
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
                approval_payload={"fields": approval_fields, "source": source},
            )

        actor_user_id = context.user_id or AUTOMATED_PIPELINE_USER_ID
        # Keep the write transaction narrow and explicit: build service once then
        # commit only after the mutate call succeeds.
        async for master_session in acquire_master_async_session():
            async for org_session in acquire_org_async_session(context.organisation_id):
                service = DealWriteService(
                    org_session=org_session,
                    master_session=master_session,
                    organisation_id=context.organisation_id,
                    actor_user_id=actor_user_id,
                )
                try:
                    deal = await service.update_deal(context.deal_id, fields)
                    await org_session.commit()
                    return _result(
                        "applied",
                        "Deal fields updated.",
                        applied_fields=fields,
                        deal_id=str(deal.deal_id),
                    )
                except DealWriteServiceError as exc:
                    await org_session.rollback()
                    return _result("failed", exc.message)
                except Exception:
                    await org_session.rollback()
                    return _result("failed", "Unexpected error while updating deal fields.")

        return _result("failed", "Could not acquire database session.")

    return update_deal_fields_tool
