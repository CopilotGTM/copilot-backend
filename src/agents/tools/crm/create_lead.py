"""Create Lead agent tool for direct chat writes."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.crm.leads import CRMLeadSourceType, CRMLeadStatus
from src.service.crm.access.leads_access_service import LeadsAccessService, LeadsAccessServiceError

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


def _normalize_email(value: str | None) -> str | None:
    cleaned = _clean_optional(value)
    return cleaned.lower() if cleaned else None


async def _rollback_if_available(session: Any) -> None:
    rollback = getattr(session, "rollback", None)
    if rollback is not None:
        await rollback()


def _lead_name(lead: Any) -> str:
    return str(
        getattr(lead, "full_name", None)
        or getattr(lead, "email", None)
        or getattr(lead, "company_name", None)
        or "lead"
    )


def _lead_result(lead: Any) -> dict[str, Any]:
    name = _lead_name(lead)
    result: dict[str, Any] = {
        "status": "created",
        "lead_id": str(lead.lead_id),
        "name": name,
        "email": getattr(lead, "email", None),
        "company_name": getattr(lead, "company_name", None),
        "message": f"Created lead **{name}**.",
    }
    for key in ("job_title", "source_channel"):
        value = getattr(lead, key, None)
        if value:
            result[key] = value
    return result


def _public_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if not key.startswith("_")}


def _channel_payload(context: AgentContext, lead: Any) -> dict[str, Any] | None:
    channel = (context.channel or "").lower()
    title = "Lead created"
    facts = [{"title": "Name", "value": _lead_name(lead)}]
    for label, attr in (("Email", "email"), ("Company", "company_name"), ("Title", "job_title")):
        value = getattr(lead, attr, None)
        if value:
            facts.append({"title": label, "value": str(value)})
    fallback = f"{title}: {_lead_name(lead)}."
    if channel in {"web", "voice"}:
        return {
            "type": "ui_card",
            "content": {
                "card": {
                    "kind": "lead_created_confirmation",
                    "version": "1.0",
                    "tone": "success",
                    "title": title,
                    "message": fallback,
                    "entity": {
                        "type": "lead",
                        "id": str(lead.lead_id),
                        "name": _lead_name(lead),
                    },
                    "facts": [{"label": fact["title"], "value": fact["value"]} for fact in facts],
                    "actions": [],
                }
            },
            "text_fallback": fallback,
            "attachments": None,
            "source_text": None,
        }
    return None


async def create_lead_direct(
    *,
    context: AgentContext,
    full_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    job_title: str | None = None,
    company_name: str | None = None,
    company_domain: str | None = None,
    message: str | None = None,
    interest: str | None = None,
) -> dict[str, Any]:
    """Create an internal CRM lead directly."""
    organisation_id = context.organisation_id
    if not organisation_id:
        return {"status": "error", "message": "Cannot create lead without an organization."}

    cleaned_name = _clean_optional(full_name)
    normalized_email = _normalize_email(email)
    cleaned_company = _clean_optional(company_name)
    if not any((cleaned_name, normalized_email, cleaned_company, _clean_optional(message))):
        return {"status": "error", "message": "Lead name, email, company, or message is required."}

    payload = {
        "source_type": CRMLeadSourceType.MANUAL,
        "source_channel": "chat",
        "status": CRMLeadStatus.NEW,
        "full_name": cleaned_name,
        "email": normalized_email,
        "phone": _clean_optional(phone),
        "job_title": _clean_optional(job_title),
        "company_name": cleaned_company,
        "company_domain": _clean_optional(company_domain),
        "message": _clean_optional(message),
        "interest": _clean_optional(interest),
    }
    payload = {key: value for key, value in payload.items() if value is not None}

    async with context.acquire_session() as org_session:
        service = LeadsAccessService(
            org_session,
            organisation_id,
            actor_user_id=context.user_id,
            master_session=None,
        )
        try:
            lead = await service.create_lead(payload)
        except LeadsAccessServiceError as exc:
            await _rollback_if_available(org_session)
            return {"status": "error", "message": f"Could not create lead: {exc.message}"}
        except Exception:
            await _rollback_if_available(org_session)
            logger.exception("create_lead_direct: unexpected failure")
            return {"status": "error", "message": "Could not create lead."}

    result = _lead_result(lead)
    payload_card = _channel_payload(context, lead)
    if payload_card:
        result[_BOT_CHANNEL_PAYLOAD_KEY] = payload_card
    return result


def create_create_lead(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the CreateLead tool bound to context."""

    async def create_lead_tool(
        full_name: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        job_title: str | None = None,
        company_name: str | None = None,
        company_domain: str | None = None,
        message: str | None = None,
        interest: str | None = None,
    ) -> dict[str, Any]:
        """Create an internal CRM lead from a captured prospect signal."""
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="CreateLead",
            tool_func=create_lead_direct,
            parameters={
                "context": context,
                "full_name": full_name,
                "email": email,
                "phone": phone,
                "job_title": job_title,
                "company_name": company_name,
                "company_domain": company_domain,
                "message": message,
                "interest": interest,
            },
            description=f"Creating lead {full_name or email or company_name or ''}".strip(),
            fallback_value={"status": "error", "message": "Could not create lead."},
        )
        return _public_tool_result(result)

    return create_lead_tool


__all__ = ["create_lead_direct", "create_create_lead"]
