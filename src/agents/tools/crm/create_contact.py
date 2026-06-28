"""Create Contact agent tool for direct chat writes."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.agents.tools.base import execute_with_capabilities
from src.service.crm.contacts.contracts import (
    ContactCreateCommand,
    ContactDealLinkServiceError,
    ContactWriteContext,
    ContactWriteServiceError,
)
from src.service.crm.contacts.deal_link_service import ContactDealLinkService
from src.service.crm.contacts.read_service import ContactReadService
from src.service.crm.contacts.write_service import ContactWriteService
from src.workers.deal_worker.client import send_deal_task_async
from src.workers.deal_worker.services.deal_change_detector import StakeholderChangeType

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

_BOT_CHANNEL_PAYLOAD_KEY = "_bot_channel_payload"


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _actor_name(context: AgentContext) -> str | None:
    value = context.metadata.get("user_name")
    return _clean_optional(value) if isinstance(value, str) else None


def _parse_uuid(value: UUID | str | None) -> UUID | None:
    if value is None or isinstance(value, UUID):
        return value
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        return UUID(cleaned)
    except ValueError:
        return None


async def _rollback_if_available(session: Any) -> None:
    rollback = getattr(session, "rollback", None)
    if rollback is not None:
        await rollback()


def _contact_name(contact: Any) -> str:
    return str(
        getattr(contact, "full_name", None)
        or getattr(contact, "name", None)
        or getattr(contact, "email", "")
    )


def _contact_result(contact: Any, *, status: str) -> dict[str, Any]:
    name = _contact_name(contact)
    result: dict[str, Any] = {
        "status": status,
        "contact_id": str(contact.contact_id),
        "email": contact.email,
        "name": name,
        "message": (
            f"Created contact **{name}**."
            if status == "created"
            else f"Contact **{name}** already exists."
        ),
    }
    for key in ("job_title", "company_name"):
        value = getattr(contact, key, None)
        if value:
            result[key] = value
    if getattr(contact, "company_id", None):
        result["company_id"] = str(contact.company_id)
    return result


def _public_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if not key.startswith("_")}


def _channel_payload(context: AgentContext, contact: Any, *, status: str) -> dict[str, Any] | None:
    channel = (context.channel or "").lower()
    title = "Contact created" if status == "created" else "Contact already exists"
    facts = [{"title": "Name", "value": _contact_name(contact)}]
    for label, attr in (("Email", "email"), ("Title", "job_title"), ("Account", "company_name")):
        value = getattr(contact, attr, None)
        if value:
            facts.append({"title": label, "value": str(value)})
    fallback = f"{title}: {_contact_name(contact)}."
    if channel in {"web", "voice"}:
        return {
            "type": "ui_card",
            "content": {
                "card": {
                    "kind": "contact_created_confirmation",
                    "version": "1.0",
                    "tone": "success",
                    "title": title,
                    "message": fallback,
                    "entity": {
                        "type": "contact",
                        "id": str(contact.contact_id),
                        "name": _contact_name(contact),
                    },
                    "facts": [{"label": fact["title"], "value": fact["value"]} for fact in facts],
                    "actions": [],
                }
            },
            "text_fallback": fallback,
            "attachments": None,
            "source_text": None,
        }
    if channel == "teams":
        return {
            "type": "message",
            "content": {
                "text": " ",
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": {
                            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                            "type": "AdaptiveCard",
                            "version": "1.4",
                            "body": [
                                {
                                    "type": "TextBlock",
                                    "text": title,
                                    "size": "Medium",
                                    "weight": "Bolder",
                                    "wrap": True,
                                    "color": "Good",
                                },
                                {"type": "FactSet", "facts": facts},
                            ],
                        },
                    }
                ],
            },
            "text_fallback": fallback,
            "attachments": None,
            "source_text": None,
        }
    if channel == "slack":
        return {
            "type": "message",
            "content": {
                "text": fallback,
                "blocks": [
                    {"type": "header", "text": {"type": "plain_text", "text": title}},
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*{fact['title']}*\n{fact['value']}"}
                            for fact in facts
                        ],
                    },
                ],
            },
            "text_fallback": fallback,
            "attachments": None,
            "source_text": None,
        }
    if channel == "whatsapp":
        return {
            "type": "message",
            "content": {
                "text": "\n".join([title, *[f"{f['title']}: {f['value']}" for f in facts]])
            },
            "text_fallback": fallback,
            "attachments": None,
            "source_text": None,
        }
    return None


async def _dispatch_stakeholder_sync(organisation_id: UUID, deal_id: UUID) -> None:
    await send_deal_task_async(
        "stakeholder.sync_deal_stakeholders",
        kwargs={
            "organisation_id": str(organisation_id),
            "deal_id": str(deal_id),
            "change_type": StakeholderChangeType.PARTICIPANT_ADDED.value,
            "integration_id": None,
            "integration_type": None,
            "trigger_rematching": True,
        },
    )


async def create_contact_direct(
    *,
    context: AgentContext,
    email: str | None,
    name: str | None,
    job_title: str | None = None,
    linkedin_url: str | None = None,
    deal_id: UUID | str | None = None,
    deal_name: str | None = None,
    relationship_role: str | None = None,
) -> dict[str, Any]:
    """Create or reuse an internal CRM contact directly."""
    organisation_id = context.organisation_id
    if not organisation_id:
        return {"status": "error", "message": "Cannot create contact without an organization."}

    cleaned_email = _clean_optional(email)
    cleaned_name = _clean_optional(name)
    if not cleaned_email:
        return {"status": "error", "message": "Contact email is required."}

    normalized_email = cleaned_email.lower()
    parsed_deal_id = _parse_uuid(deal_id)
    if deal_id and parsed_deal_id is None:
        return {"status": "error", "message": "deal_id must be a valid UUID."}

    async with context.acquire_session() as org_session:
        contact_reader = ContactReadService(org_session, organisation_id)
        contact_writer = ContactWriteService(org_session, organisation_id)
        deal_linker = ContactDealLinkService(org_session, organisation_id)
        try:
            contact = await contact_reader.find_contact_by_email(normalized_email)
            status = "exists" if contact else "created"

            if not contact:
                contact_result = await contact_writer.create_contact(
                    ContactCreateCommand(
                        email=normalized_email,
                        name=cleaned_name,
                        job_title=_clean_optional(job_title),
                        linkedin_url=_clean_optional(linkedin_url),
                    ),
                    context=ContactWriteContext(
                        source="agent_tool",
                        actor_user_id=context.user_id,
                        actor_name=_actor_name(context),
                        actor_email=context.user_email,
                        created_via="chat_tool",
                    ),
                )
                contact = contact_result.contact
                await org_session.commit()

            result = _contact_result(contact, status=status)

            if parsed_deal_id:
                link_result = await deal_linker.add_contact_to_deal(
                    contact_id=contact.contact_id,
                    deal_id=parsed_deal_id,
                    relationship_role=_clean_optional(relationship_role),
                    actor_user_id=context.user_id,
                )
                await org_session.commit()
                link = {
                    "created": link_result.created,
                    "skipped": link_result.skipped,
                    "contact_participant_changed": link_result.contact_participant_changed,
                }
                if link["created"]:
                    try:
                        await _dispatch_stakeholder_sync(organisation_id, parsed_deal_id)
                    except Exception:
                        logger.exception("create_contact_direct: stakeholder sync dispatch failed")
                    result["deal_link_status"] = "linked"
                else:
                    result["deal_link_status"] = "already_linked"
                result["deal_id"] = str(parsed_deal_id)
                if _clean_optional(deal_name):
                    result["deal_name"] = _clean_optional(deal_name)
        except (ContactWriteServiceError, ContactDealLinkServiceError) as exc:
            await _rollback_if_available(org_session)
            return {"status": "error", "message": f"Could not create contact: {exc.message}"}
        except Exception:
            await _rollback_if_available(org_session)
            logger.exception("create_contact_direct: unexpected failure")
            return {"status": "error", "message": "Could not create contact."}

    payload = _channel_payload(context, contact, status=status)
    if payload:
        result[_BOT_CHANNEL_PAYLOAD_KEY] = payload
    return result


def create_create_contact(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the CreateContact tool bound to context."""

    async def create_contact_tool(
        email: str | None = None,
        name: str | None = None,
        job_title: str | None = None,
        linkedin_url: str | None = None,
        deal_id: str | None = None,
        deal_name: str | None = None,
        relationship_role: str | None = None,
    ) -> dict[str, Any]:
        """Create an internal CRM contact directly once email is known."""
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="CreateContact",
            tool_func=create_contact_direct,
            parameters={
                "context": context,
                "email": email,
                "name": name,
                "job_title": job_title,
                "linkedin_url": linkedin_url,
                "deal_id": deal_id,
                "deal_name": deal_name,
                "relationship_role": relationship_role,
            },
            description=f"Creating contact {name or email or ''}".strip(),
            fallback_value={"status": "error", "message": "Could not create contact."},
        )
        return _public_tool_result(result)

    return create_contact_tool


__all__ = ["create_contact_direct", "create_create_contact"]
