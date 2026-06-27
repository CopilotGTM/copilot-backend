"""Create Account agent tool for direct chat writes."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.agents.tools.base import execute_with_capabilities
from src.service.crm.accounts.contracts import (
    AccountCreateCommand,
    AccountWriteContext,
    AccountWriteServiceError,
)
from src.service.crm.accounts.read_service import AccountReadService
from src.service.crm.accounts.write_service import AccountWriteService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

_BOT_CHANNEL_PAYLOAD_KEY = "_bot_channel_payload"
_DOMAIN_PROTOCOL_RE = re.compile(r"^https?://", re.IGNORECASE)
_DOMAIN_WWW_RE = re.compile(r"^www\.", re.IGNORECASE)


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalize_domain(domain: str | None) -> str | None:
    cleaned = _clean_optional(domain)
    if not cleaned:
        return None
    cleaned = _DOMAIN_PROTOCOL_RE.sub("", cleaned)
    cleaned = _DOMAIN_WWW_RE.sub("", cleaned)
    cleaned = cleaned.split("/")[0].split("?")[0].split("#")[0]
    return cleaned.lower() or None


async def _rollback_if_available(session: Any) -> None:
    rollback = getattr(session, "rollback", None)
    if rollback is not None:
        await rollback()


def _account_name(account: Any) -> str:
    return str(getattr(account, "name", "") or "")


def _account_result(account: Any, *, status: str) -> dict[str, Any]:
    name = _account_name(account)
    result: dict[str, Any] = {
        "status": status,
        "account_id": str(account.company_id),
        "name": name,
        "message": (
            f"Created account **{name}**."
            if status == "created"
            else f"Account **{name}** already exists."
        ),
    }
    for key in ("domain", "website", "industry", "linkedin_url"):
        value = getattr(account, key, None)
        if value:
            result[key] = value
    return result


def _public_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if not key.startswith("_")}


def _channel_payload(context: AgentContext, account: Any, *, status: str) -> dict[str, Any] | None:
    channel = (context.channel or "").lower()
    title = "Account created" if status == "created" else "Account already exists"
    facts = [{"title": "Name", "value": _account_name(account)}]
    for label, attr in (("Domain", "domain"), ("Website", "website"), ("Industry", "industry")):
        value = getattr(account, attr, None)
        if value:
            facts.append({"title": label, "value": str(value)})
    fallback = f"{title}: {_account_name(account)}."

    if channel in {"web", "voice"}:
        return {
            "type": "ui_card",
            "content": {
                "card": {
                    "kind": "account_created_confirmation",
                    "version": "1.0",
                    "tone": "success",
                    "title": title,
                    "message": fallback,
                    "entity": {
                        "type": "account",
                        "id": str(account.company_id),
                        "name": _account_name(account),
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


async def create_account_direct(
    *,
    context: AgentContext,
    name: str | None,
    domain: str | None = None,
    website: str | None = None,
    industry: str | None = None,
    linkedin_url: str | None = None,
) -> dict[str, Any]:
    """Create or reuse an internal CRM account directly."""
    organisation_id = context.organisation_id
    if not organisation_id:
        return {"status": "error", "message": "Cannot create account without an organization."}

    cleaned_name = _clean_optional(name)
    cleaned_website = _clean_optional(website)
    normalized_domain = _normalize_domain(domain or cleaned_website)
    if not cleaned_name and not normalized_domain and not cleaned_website:
        return {"status": "error", "message": "Account name, domain, or website is required."}

    command = AccountCreateCommand(
        name=cleaned_name,
        domain=normalized_domain,
        website=cleaned_website,
        industry=_clean_optional(industry),
        linkedin_url=_clean_optional(linkedin_url),
    )

    async with context.acquire_session() as org_session:
        reader = AccountReadService(org_session, organisation_id)
        writer = AccountWriteService(org_session, organisation_id)
        try:
            if normalized_domain:
                existing = await reader.find_accounts_by_domain(normalized_domain)
            else:
                if cleaned_name is None:
                    return {
                        "status": "error",
                        "message": "Account name, domain, or website is required.",
                    }
                existing = await reader.find_accounts_by_name(cleaned_name)
            if existing:
                result = _account_result(existing[0], status="exists")
            else:
                write_result = await writer.create_account(
                    command,
                    context=AccountWriteContext(
                        source="agent_tool",
                        actor_user_id=context.user_id,
                        created_via="chat_tool",
                        duplicate_scope="internal_only",
                    ),
                )
                account = write_result.account
                await org_session.commit()
                result = _account_result(account, status="created")
        except AccountWriteServiceError as exc:
            await _rollback_if_available(org_session)
            return {"status": "error", "message": f"Could not create account: {exc.message}"}
        except Exception:
            await _rollback_if_available(org_session)
            logger.exception("create_account_direct: unexpected failure")
            return {"status": "error", "message": "Could not create account."}

    payload = _channel_payload(
        context, existing[0] if result["status"] == "exists" else account, status=result["status"]
    )
    if payload:
        result[_BOT_CHANNEL_PAYLOAD_KEY] = payload
    return result


def create_create_account(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the CreateAccount tool bound to context."""

    async def create_account_tool(
        name: str | None = None,
        domain: str | None = None,
        website: str | None = None,
        industry: str | None = None,
        linkedin_url: str | None = None,
    ) -> dict[str, Any]:
        """Create an internal CRM account once a name, domain, or website is known."""
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="CreateAccount",
            tool_func=create_account_direct,
            parameters={
                "context": context,
                "name": name,
                "domain": domain,
                "website": website,
                "industry": industry,
                "linkedin_url": linkedin_url,
            },
            description=f"Creating account {name or ''}".strip(),
            fallback_value={"status": "error", "message": "Could not create account."},
        )
        return _public_tool_result(result)

    return create_account_tool


__all__ = ["create_account_direct", "create_create_account"]
