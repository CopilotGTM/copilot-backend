"""Agent tool for updating CRM account/company fields on a context-bound deal.

The returned tool is used by AI-run CRM workflows and expects both
``organisation_id`` and ``deal_id`` to be present in the AgentContext.
This keeps tenant isolation explicit: caller-provided IDs are never treated
as trust anchors on their own.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.service.crm.deals.contracts import DealWriteServiceError
from src.service.crm.deals.write_service import DealWriteService
from src.service.crm.internal.deal_update_policy import classify_deal_update_policy

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
ALLOWED_ACCOUNT_FIELDS = {"website", "domain", "linkedin_url", "industry"}


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
        "action": "update_deal_account",
        "reason": reason,
        "applied_fields": applied_fields or {},
        "requires_approval": status == "approval_required",
        "approval_payload": approval_payload,
    }
    if deal_id:
        result["deal_id"] = deal_id
    return result


def create_update_deal_account(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Build the `update_deal_account` tool for the currently scoped deal.

    The tool enforces tenant + deal context, validates supported fields, applies
    the approval policy, and only then mutates CRM state through
    ``DealWriteService``.
    """

    async def update_deal_account_tool(
        account: dict[str, Any],
        directly_requested: bool = False,
        source: str = "agent",
    ) -> dict[str, Any]:
        """Apply incoming account/company updates with policy-aware short-circuiting."""
        _ = capabilities_getter
        # Security boundary: both org and deal must come from runtime context.
        if not context.organisation_id or not context.deal_id:
            return _result("failed", "Deal context is required.")
        if not account:
            return _result("failed", "At least one account field is required.")

        unsupported = sorted(set(account) - ALLOWED_ACCOUNT_FIELDS)
        if unsupported:
            return _result("skipped", f"Account fields are not supported: {', '.join(unsupported)}")

        approval_fields: dict[str, Any] = {}
        # Validate every requested field before writing so mixed requests fail in
        # one coherent response instead of applying partial updates.
        for field, new_value in account.items():
            decision = classify_deal_update_policy(
                action="update_deal_account",
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
            # Mixed updates can include safe and sensitive fields together.
            # Route only the sensitive subset through approval instead of
            # dropping the entire tool call.
            return _result(
                "approval_required",
                "One or more account updates require approval.",
                approval_payload={"account": approval_fields, "source": source},
            )

        actor_user_id = context.user_id or AUTOMATED_PIPELINE_USER_ID
        # Keep this action in the smallest possible DB transaction.
        async for master_session in acquire_master_async_session():
            async for org_session in acquire_org_async_session(context.organisation_id):
                service = DealWriteService(
                    org_session=org_session,
                    master_session=master_session,
                    organisation_id=context.organisation_id,
                    actor_user_id=actor_user_id,
                )
                # Guard write consistency by committing only after the service
                # mutation succeeds; rollback every failure path.
                try:
                    deal = await service.update_account(context.deal_id, account)
                    await org_session.commit()
                    return _result(
                        "applied",
                        "Deal account updated.",
                        applied_fields=account,
                        deal_id=str(deal.deal_id),
                    )
                except DealWriteServiceError as exc:
                    await org_session.rollback()
                    return _result("failed", exc.message)
                except Exception:
                    await org_session.rollback()
                    return _result("failed", "Unexpected error while updating deal account.")

        return _result("failed", "Could not acquire database session.")

    return update_deal_account_tool
