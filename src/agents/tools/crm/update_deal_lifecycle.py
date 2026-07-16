"""Agent tool for lifecycle transitions on an explicitly resolved deal.

This tool handles safe lifecycle actions (stage move and close/reopen flows) and
forces policy checks before any mutating write, so inferred changes can be held
for approval instead of silently changing deal state. Organization context is
the tenant boundary; callers pass the deal ID returned by search or creation.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.agents.tools.crm.update_deal_fields import _deal_context_fields, _resolve_deal_id
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.service.crm.deals.contracts import DealWriteServiceError
from src.service.crm.deals.write_service import DealWriteService
from src.service.crm.internal.deal_update_policy import classify_deal_update_policy

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
SUPPORTED_ACTIONS = {"move_stage", "close_won", "close_lost", "reopen"}


def _result(
    status: str,
    reason: str,
    *,
    approval_payload: dict[str, Any] | None = None,
    deal_id: str | None = None,
    lifecycle_action: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "action": "update_deal_lifecycle",
        "reason": reason,
        "requires_approval": status == "approval_required",
        "approval_payload": approval_payload,
    }
    if deal_id:
        result["deal_id"] = deal_id
    if lifecycle_action:
        result["lifecycle_action"] = lifecycle_action
    return result


def create_update_deal_lifecycle(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Build the organization-scoped `update_deal_lifecycle` tool.

    Callers resolve the deal first. The returned callable validates the ID and
    action, then routes through DealWriteService for authoritative transitions.
    """

    async def update_deal_lifecycle_tool(
        deal_id: str,
        action: str,
        stage_id: str | None = None,
        closed_reason: str | None = None,
        close_date: str | None = None,
        clear_closed_reason: bool = False,
        directly_requested: bool = False,
        source: str = "agent",
    ) -> dict[str, Any]:
        """Apply a lifecycle action to one resolved deal.

        In general chat, first use ``search_deals`` and disambiguate multiple
        results. Deal-scoped workflows pass their assigned deal ID. Approval
        payloads retain that ID for safe replay. Lifecycle transitions remain
        approval-gated even when the user directly requests them.
        """
        _ = capabilities_getter
        if not context.organisation_id:
            return _result("failed", "Organisation context is required.")
        try:
            resolved_deal_id = _resolve_deal_id(context, deal_id)
        except ValueError as exc:
            return _result("failed", str(exc))
        if not action:
            return _result("failed", "Lifecycle action is required.")
        if action not in SUPPORTED_ACTIONS:
            return _result("skipped", f"Lifecycle action '{action}' is not supported.")
        if action == "move_stage" and not stage_id:
            return _result("failed", "stage_id is required for move_stage.")
        if action == "close_won":
            closed_reason = None
        elif isinstance(closed_reason, str):
            closed_reason = closed_reason.strip() or None
        if action == "close_lost" and not closed_reason:
            return _result("skipped", "closed_reason is required for close_lost.")

        try:
            # Validate proposed lifecycle payload before approval so operators
            # only review values that could actually be applied.
            parsed_stage_id = UUID(stage_id) if stage_id else None
            parsed_close_date = datetime.fromisoformat(close_date) if close_date else None
        except ValueError:
            return _result("failed", "Invalid stage_id or close_date format.")

        policy_field = "outcome" if action in {"close_won", "close_lost"} else "stage_id"
        # Close/reopen operations change state invariants for a deal; classify first
        # so user-visible approval can block unsafe transitions before write.
        decision = classify_deal_update_policy(
            action="update_deal_lifecycle",
            field=policy_field,
            old_value=None,
            new_value=action,
            source=source,
            directly_requested=directly_requested,
        )
        if decision.status == "skipped":
            return _result("skipped", decision.reason)
        if decision.requires_approval:
            return _result(
                "approval_required",
                decision.reason,
                approval_payload={
                    "deal_id": str(resolved_deal_id),
                    "action": action,
                    "stage_id": stage_id,
                    "closed_reason": closed_reason,
                    "close_date": close_date,
                    "clear_closed_reason": clear_closed_reason,
                    "source": source,
                },
            )

        actor_user_id = context.user_id or AUTOMATED_PIPELINE_USER_ID
        # Keep lifecycle mutation atomic: mutate then commit, rollback on all
        # error paths to avoid mixed outcome state.
        async for master_session in acquire_master_async_session():
            async for org_session in acquire_org_async_session(context.organisation_id):
                service = DealWriteService(
                    org_session=org_session,
                    master_session=master_session,
                    organisation_id=context.organisation_id,
                    actor_user_id=actor_user_id,
                )
                try:
                    if action == "move_stage":
                        deal = await service.move_stage(resolved_deal_id, parsed_stage_id)
                    elif action == "close_won":
                        deal = await service.close_deal(
                            resolved_deal_id,
                            outcome="won",
                            closed_reason=closed_reason,
                            close_date=parsed_close_date,
                            stage_id=parsed_stage_id,
                        )
                    elif action == "close_lost":
                        deal = await service.close_deal(
                            resolved_deal_id,
                            outcome="lost",
                            closed_reason=closed_reason,
                            close_date=parsed_close_date,
                            stage_id=parsed_stage_id,
                        )
                    else:
                        deal = await service.reopen_deal(
                            resolved_deal_id,
                            stage_id=parsed_stage_id,
                            clear_closed_reason=clear_closed_reason,
                        )
                    await org_session.commit()
                    return _result(
                        "applied",
                        "Deal lifecycle updated.",
                        deal_id=str(deal.deal_id),
                        lifecycle_action=action,
                    ) | _deal_context_fields(deal)
                except DealWriteServiceError as exc:
                    await org_session.rollback()
                    return _result("failed", exc.message)
                except Exception:
                    await org_session.rollback()
                    return _result("failed", "Unexpected error while updating deal lifecycle.")

        return _result("failed", "Could not acquire database session.")

    return update_deal_lifecycle_tool
