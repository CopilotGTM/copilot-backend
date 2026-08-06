"""Agent-facing contact update tool for internal CRM records.

Called through `ToolRegistry` by chat-style agents after a contact has already
been identified by `contact_id`. The immutable boundary is the
`AgentContext.organisation_id`: tool callers may supply a contact ID, but the
tool only updates active contacts that belong to that org and to the internal
CRM integration.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.service.crm.contacts.contracts import ContactWriteContext, ContactWriteServiceError
from src.service.crm.contacts.write_service import ContactWriteService
from src.service.crm.internal.internal_crm_setup_service import (
    AUTOMATED_PIPELINE_USER_ID,
    InternalCRMSetupService,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

# Keep the agent field surface narrower than the underlying service. In
# particular, `company_name` is excluded so an agent cannot alter a presentation
# snapshot without linking the contact to a real account through `company_id`.
SUPPORTED_CONTACT_UPDATE_FIELDS = {
    "email",
    "first_name",
    "last_name",
    "full_name",
    "job_title",
    "phone",
    "mobile_phone",
    "linkedin_url",
    "company_id",
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
        return {}, "At least one contact update field is required."
    unsupported = sorted(set(fields) - SUPPORTED_CONTACT_UPDATE_FIELDS)
    if unsupported:
        return {}, "Unsupported contact update fields: " + ", ".join(unsupported)
    normalized = {field: _clean_optional(value) for field, value in fields.items()}
    if not normalized:
        return {}, "At least one contact update field is required."
    return normalized, None


def _contact_name(contact: Any) -> str:
    """Choose the most stable human-readable label for chat responses."""
    return str(
        getattr(contact, "full_name", None)
        or getattr(contact, "name", None)
        or getattr(contact, "email", "")
    )


def _contact_result(contact: Any, updated_fields: dict[str, Any]) -> dict[str, Any]:
    """Shape the write result for agents without exposing ORM internals."""
    result: dict[str, Any] = {
        "status": "updated",
        "contact_id": str(contact.contact_id),
        "name": _contact_name(contact),
        "email": getattr(contact, "email", None),
        "updated_fields": updated_fields,
        "message": f"Updated contact **{_contact_name(contact)}**.",
    }
    for key in ("first_name", "last_name", "job_title", "phone", "mobile_phone", "company_name"):
        value = getattr(contact, key, None)
        if value:
            result[key] = value
    if getattr(contact, "company_id", None):
        result["company_id"] = str(contact.company_id)
    return result


async def _internal_contact_guard(
    *,
    context: AgentContext,
    org_session: Any,
    contact_id: UUID,
) -> str | None:
    """Reject cross-org, deleted, or provider-synced contacts before mutation.

    The write service already enforces org ownership, but the agent tool has a
    stricter product boundary: agents may update Copilot-owned internal CRM rows,
    not local snapshots imported from HubSpot/Salesforce that could be
    overwritten by the next provider sync.
    """
    # The tool receives `contact_id` from the model/tool call. The org check is
    # the tenant boundary; the external ID alone is never trusted for access.
    contact = await org_session.get(CRMContact, contact_id)
    if (
        contact is None
        or contact.organisation_id != context.organisation_id
        or getattr(contact, "is_deleted", False)
    ):
        return "Contact not found"

    integration = await InternalCRMSetupService(
        org_session,
        context.organisation_id,
        context.user_id or AUTOMATED_PIPELINE_USER_ID,
    ).get_internal_integration()
    if not integration or contact.integration_id != integration.integration_id:
        return "Agent contact updates are limited to internal CRM contacts."
    return None


async def update_contact_direct(
    *,
    context: AgentContext,
    contact_id: UUID | str | None,
    fields: dict[str, Any] | None,
) -> dict[str, Any]:
    """Update one known internal CRM contact through the canonical write service.

    Required context: `organisation_id` must come from `AgentContext`, and the
    caller must pass a concrete `contact_id` plus supported fields. The function
    owns the tool transaction: preflight, write, commit on success, rollback on
    service or unexpected errors.
    """
    organisation_id = context.organisation_id
    if not organisation_id:
        return {"status": "error", "message": "Cannot update contact without an organization."}

    parsed_contact_id, id_error = _parse_uuid(contact_id, "contact_id")
    if id_error:
        return {"status": "error", "message": id_error}
    # `_parse_uuid` returns `(None, error)` for invalid input; after the early
    # return this assertion documents the invariant for static type checkers.
    assert parsed_contact_id is not None

    normalized_fields, fields_error = _normalize_fields(fields)
    if fields_error:
        return {"status": "error", "message": fields_error}

    async with context.acquire_session() as org_session:
        try:
            # Preflight before constructing the writer so provider-synced rows
            # fail without entering the mutation/event-recording path.
            guard_error = await _internal_contact_guard(
                context=context,
                org_session=org_session,
                contact_id=parsed_contact_id,
            )
            if guard_error:
                return {"status": "error", "message": guard_error}

            service = ContactWriteService(org_session, organisation_id)
            result = await service.update_contact(
                parsed_contact_id,
                normalized_fields,
                context=ContactWriteContext(
                    source="agent_tool",
                    actor_user_id=context.user_id,
                    actor_name=_actor_name(context),
                    actor_email=context.user_email,
                    created_via="chat_tool",
                    **context.automation_causation(),
                ),
            )
            # Commit after the service returns so mutation events and row changes
            # become durable together; returning before commit would lie to chat.
            await org_session.commit()
            return _contact_result(result.contact, normalized_fields)
        except ContactWriteServiceError as exc:
            # Service errors are expected business failures, so translate them
            # into a tool result instead of raising into the agent loop.
            await _rollback_if_available(org_session)
            return {"status": "error", "message": f"Could not update contact: {exc.message}"}
        except Exception:
            await _rollback_if_available(org_session)
            logger.exception("update_contact_direct: unexpected failure")
            return {"status": "error", "message": "Could not update contact."}


def create_update_contact(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Build the `update_contact` tool for the current agent context.

    The bound context supplies the immutable organisation and actor metadata.
    Tool callers only provide the target contact ID and fields; they cannot
    override tenant scope through parameters.
    """

    async def update_contact_tool(
        contact_id: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update a known internal CRM contact after the user identifies the record.

        Use this only after search or UI context has selected a specific
        `contact_id`. Do not use it to infer and update a contact from a fuzzy
        name or email in the same call.
        """
        return dict(
            await execute_with_capabilities(
                capabilities_getter(),
                tool_name="UpdateContact",
                tool_func=update_contact_direct,
                parameters={"context": context, "contact_id": contact_id, "fields": fields},
                description=f"Updating contact {contact_id or ''}".strip(),
                fallback_value={"status": "error", "message": "Could not update contact."},
            )
        )

    return update_contact_tool


__all__ = ["create_update_contact", "update_contact_direct"]
