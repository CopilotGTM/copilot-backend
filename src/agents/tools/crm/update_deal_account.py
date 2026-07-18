"""Agent tool for updating account/company fields on a resolved CRM deal.

The organization ID remains immutable in AgentContext, while callers pass a
deal ID returned by search or creation. The write service validates and mutates
the target inside that tenant boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from src.agents.tools.crm.access import AgentCRMAccessError, resolve_agent_access_actor
from src.agents.tools.crm.update_deal_fields import _deal_context_fields, _resolve_deal_id
from src.contracts.crm.deal_write import DealAccountUpdateRequest
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.service.crm.deals.contracts import DealWriteServiceError
from src.service.crm.deals.write_service import DealWriteService
from src.service.crm.internal.deal_update_policy import classify_deal_update_policy

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
ALLOWED_ACCOUNT_FIELDS = {"name", "domain", "website", "linkedin_url", "industry", "country"}


class DealAccountUpdateInput(BaseModel):
    """LLM-facing profile fields accepted by the deal account update action."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    domain: str | None = None
    website: str | None = None
    linkedin_url: str | None = None
    industry: str | None = None
    country: str | None = None


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
    """Build the organization-scoped `update_deal_account` tool.

    Callers resolve the target deal first. The tool validates that ID, applies
    field and approval policy, then mutates through ``DealWriteService``.
    """

    async def update_deal_account_tool(
        deal_id: str,
        account: DealAccountUpdateInput,
        directly_requested: bool = False,
        source: str = "agent",
    ) -> dict[str, Any]:
        """Update account/company fields for one resolved deal.

        In general chat, first use ``search_deals`` and disambiguate multiple
        results. Deal-scoped workflows pass their assigned deal ID. Include only
        the requested account fields.

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
        if isinstance(account, dict):
            unsupported = sorted(set(account) - ALLOWED_ACCOUNT_FIELDS)
            if unsupported:
                return _result(
                    "skipped",
                    f"Account fields are not supported: {', '.join(unsupported)}",
                )
        try:
            payload = (
                account if isinstance(account, dict) else account.model_dump(exclude_unset=True)
            )
            request = DealAccountUpdateRequest(**payload)
            normalized_account = request.model_dump(exclude_unset=True)
        except ValidationError as exc:
            errors = exc.errors()
            reason = str(errors[0]["msg"]) if errors else "Invalid account field payload."
            return _result("failed", reason)
        if not normalized_account:
            return _result("failed", "At least one account field is required.")

        unsupported = sorted(set(normalized_account) - ALLOWED_ACCOUNT_FIELDS)
        if unsupported:
            return _result("skipped", f"Account fields are not supported: {', '.join(unsupported)}")

        effective_account = (
            normalized_account
            if directly_requested
            else {field: value for field, value in normalized_account.items() if value is not None}
        )
        if not effective_account:
            return _result("skipped", "No new value was provided for the update.")

        approval_fields: dict[str, Any] = {}
        # Validate every requested field before writing so mixed requests fail in
        # one coherent response instead of applying partial updates.
        for field, new_value in effective_account.items():
            if new_value is None:
                continue
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
                approval_payload={
                    "deal_id": str(resolved_deal_id),
                    "account": approval_fields,
                    "source": source,
                },
            )

        actor_user_id = context.user_id or AUTOMATED_PIPELINE_USER_ID
        try:
            access_actor = await resolve_agent_access_actor(context)
        except AgentCRMAccessError as exc:
            return _result("failed", str(exc))
        # Keep this action in the smallest possible DB transaction.
        async for master_session in acquire_master_async_session():
            async for org_session in acquire_org_async_session(context.organisation_id):
                service = DealWriteService(
                    org_session=org_session,
                    master_session=master_session,
                    organisation_id=context.organisation_id,
                    actor_user_id=actor_user_id,
                    access_actor=access_actor,
                )
                # Guard write consistency by committing only after the service
                # mutation succeeds; rollback every failure path.
                try:
                    deal = await service.update_account(resolved_deal_id, effective_account)
                    await org_session.commit()
                    return _result(
                        "applied",
                        "Deal account updated.",
                        applied_fields=effective_account,
                        deal_id=str(deal.deal_id),
                    ) | _deal_context_fields(deal)
                except DealWriteServiceError as exc:
                    await org_session.rollback()
                    return _result("failed", exc.message)
                except Exception:
                    await org_session.rollback()
                    return _result("failed", "Unexpected error while updating deal account.")

        return _result("failed", "Could not acquire database session.")

    return update_deal_account_tool
