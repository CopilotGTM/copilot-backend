"""Agent-facing account update tool for internal CRM records.

Called through `ToolRegistry` by chat-style agents after an account has already
been identified by `account_id`. The immutable boundary is the
`AgentContext.organisation_id`: tool callers may supply an account ID, but the
tool only updates active accounts that belong to that org and to the internal
CRM integration.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.service.crm.accounts.contracts import AccountWriteContext, AccountWriteServiceError
from src.service.crm.accounts.write_service import AccountWriteService
from src.service.crm.internal.internal_crm_setup_service import (
    AUTOMATED_PIPELINE_USER_ID,
    InternalCRMSetupService,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

# Keep the agent field surface explicit so prompt output cannot smuggle owner or
# provider-managed fields into the shared account writer.
SUPPORTED_ACCOUNT_UPDATE_FIELDS = {
    "name",
    "domain",
    "website",
    "industry",
    "lifecycle_stage",
    "company_type",
    "description",
    "phone",
    "linkedin_url",
    "number_of_employees",
    "annual_revenue",
    "address",
    "city",
    "state",
    "postal_code",
    "country",
    "timezone",
}


def _clean_optional(value: Any) -> Any:
    """Normalize user-provided text while preserving non-string values."""
    if isinstance(value, str):
        return value.strip() or None
    return value


def _actor_name(context: AgentContext) -> str | None:
    """Read optional actor display metadata without trusting prompts for identity."""
    value = context.metadata.get("user_name")
    cleaned = _clean_optional(value)
    return cleaned if isinstance(cleaned, str) else None


def _parse_uuid(value: UUID | str | None, field_name: str) -> tuple[UUID | None, str | None]:
    """Parse tool-supplied IDs into UUIDs and return user-safe validation errors."""
    if value is None:
        return None, f"{field_name} is required."
    if isinstance(value, UUID):
        return value, None
    cleaned = value.strip()
    if not cleaned:
        return None, f"{field_name} is required."
    try:
        return UUID(cleaned), None
    except ValueError:
        return None, f"{field_name} must be a valid UUID."


async def _rollback_if_available(session: Any) -> None:
    """Rollback test doubles and real sessions through the same optional hook."""
    rollback = getattr(session, "rollback", None)
    if rollback is not None:
        await rollback()


def _normalize_fields(fields: dict[str, Any] | None) -> tuple[dict[str, Any], str | None]:
    """Apply the agent update allow-list before the shared writer sees the payload."""
    if not fields:
        return {}, "At least one account update field is required."
    unsupported = sorted(set(fields) - SUPPORTED_ACCOUNT_UPDATE_FIELDS)
    if unsupported:
        return {}, "Unsupported account update fields: " + ", ".join(unsupported)
    normalized = {field: _clean_optional(value) for field, value in fields.items()}
    if not normalized:
        return {}, "At least one account update field is required."
    return normalized, None


def _account_name(account: Any) -> str:
    """Choose the most stable human-readable label for chat responses."""
    return str(getattr(account, "name", "") or "")


def _account_result(account: Any, updated_fields: dict[str, Any]) -> dict[str, Any]:
    """Shape the write result for agents without exposing ORM internals."""
    result: dict[str, Any] = {
        "status": "updated",
        "account_id": str(account.company_id),
        "name": _account_name(account),
        "updated_fields": updated_fields,
        "message": f"Updated account **{_account_name(account)}**.",
    }
    for key in ("domain", "website", "industry", "lifecycle_stage", "company_type", "phone"):
        value = getattr(account, key, None)
        if value:
            result[key] = value
    return result


async def _internal_account_guard(
    *,
    context: AgentContext,
    org_session: Any,
    account_id: UUID,
) -> str | None:
    """Reject cross-org, deleted, or provider-synced accounts before mutation.

    The write service already enforces org ownership, but the agent tool has a
    stricter product boundary: agents may update Copilot-owned internal CRM rows,
    not local snapshots imported from HubSpot/Salesforce that could be
    overwritten by the next provider sync.
    """
    # The tool receives `account_id` from the model/tool call. The org check is
    # the tenant boundary; the external ID alone is never trusted for access.
    account = await org_session.get(CRMCompany, account_id)
    if (
        account is None
        or account.organisation_id != context.organisation_id
        or getattr(account, "is_deleted", False)
    ):
        return "Account not found"

    integration = await InternalCRMSetupService(
        org_session,
        context.organisation_id,
        context.user_id or AUTOMATED_PIPELINE_USER_ID,
    ).get_internal_integration()
    if not integration or account.integration_id != integration.integration_id:
        return "Agent account updates are limited to internal CRM accounts."
    return None


async def update_account_direct(
    *,
    context: AgentContext,
    account_id: UUID | str | None,
    fields: dict[str, Any] | None,
) -> dict[str, Any]:
    """Update one known internal CRM account through the canonical write service.

    Required context: `organisation_id` must come from `AgentContext`, and the
    caller must pass a concrete `account_id` plus supported fields. The function
    owns the tool transaction: preflight, write, commit on success, rollback on
    service or unexpected errors.
    """
    organisation_id = context.organisation_id
    if not organisation_id:
        return {"status": "error", "message": "Cannot update account without an organization."}

    parsed_account_id, id_error = _parse_uuid(account_id, "account_id")
    if id_error:
        return {"status": "error", "message": id_error}
    # `_parse_uuid` returns `(None, error)` for invalid input; after the early
    # return this assertion documents the invariant for static type checkers.
    assert parsed_account_id is not None

    normalized_fields, fields_error = _normalize_fields(fields)
    if fields_error:
        return {"status": "error", "message": fields_error}

    async with context.acquire_session() as org_session:
        try:
            # Preflight before constructing the writer so provider-synced rows
            # fail without entering the mutation/event-recording path.
            guard_error = await _internal_account_guard(
                context=context,
                org_session=org_session,
                account_id=parsed_account_id,
            )
            if guard_error:
                return {"status": "error", "message": guard_error}

            service = AccountWriteService(org_session, organisation_id)
            account = await service.update_account(
                parsed_account_id,
                normalized_fields,
                context=AccountWriteContext(
                    source="agent_tool",
                    actor_user_id=context.user_id,
                    actor_name=_actor_name(context),
                    actor_email=context.user_email,
                    created_via="chat_tool",
                    duplicate_scope="strict_org",
                ),
            )
            # Commit after the service returns so mutation events and row changes
            # become durable together; returning before commit would lie to chat.
            await org_session.commit()
            return _account_result(account, normalized_fields)
        except AccountWriteServiceError as exc:
            # Service errors are expected business failures, so translate them
            # into a tool result instead of raising into the agent loop.
            await _rollback_if_available(org_session)
            return {"status": "error", "message": f"Could not update account: {exc.message}"}
        except Exception:
            await _rollback_if_available(org_session)
            logger.exception("update_account_direct: unexpected failure")
            return {"status": "error", "message": "Could not update account."}


def create_update_account(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Build the `update_account` tool for the current agent context.

    The bound context supplies the immutable organisation and actor metadata.
    Tool callers only provide the target account ID and fields; they cannot
    override tenant scope through parameters.
    """

    async def update_account_tool(
        account_id: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update a known internal CRM account after the user identifies the record.

        Use this only after search or UI context has selected a specific
        `account_id`. Do not use it to infer and update an account from a fuzzy
        name or domain in the same call.
        """
        return dict(
            await execute_with_capabilities(
                capabilities_getter(),
                tool_name="UpdateAccount",
                tool_func=update_account_direct,
                parameters={"context": context, "account_id": account_id, "fields": fields},
                description=f"Updating account {account_id or ''}".strip(),
                fallback_value={"status": "error", "message": "Could not update account."},
            )
        )

    return update_account_tool


__all__ = ["create_update_account", "update_account_direct"]
