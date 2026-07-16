"""Create Deal agent tool and legacy confirmation-card fallback.

The normal agent path creates an internal CRM deal directly once the user has
provided a deal name. The card/form helpers remain for previously sent Teams
and Slack cards, whose submissions are still handled by CreateDealFormHandler
in src/service/bots/form_handlers/.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access

This is an ORG-SCOPED tool. deal_id is NOT required.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col

from src.agents.tools.base import execute_with_capabilities
from src.agents.tools.crm.policy import ensure_crm_write_scope
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealStage,
    CRMPipeline,
)
from src.infra.database.sessions import acquire_master_async_session
from src.service.bots.form_handlers.nonce import create_nonce
from src.service.bots.teams_bot_service import TeamsBotService
from src.service.crm.deals.contracts import DealWriteServiceError
from src.service.crm.deals.custom_field_service import DealCustomFieldWriteService
from src.service.crm.deals.write_service import DealWriteService
from src.service.crm.schema.deal_schema_service import DealSchemaService
from src.service.orgnisation.organisation_policy_service import OrganisationPolicyDeniedError

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


def _parse_expected_close_date(value: str | None) -> datetime | None:
    cleaned = _clean_optional(value)
    if cleaned is None:
        return None

    normalized = cleaned.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        logger.warning("create_deal_direct: invalid expected_close_date: %s", value)
        return None


def _created_deal_result(deal: Any) -> dict[str, Any]:
    deal_id = str(deal.deal_id)
    result: dict[str, Any] = {
        "status": "created",
        "deal_id": deal_id,
        "name": deal.name,
        "pipeline_name": deal.pipeline_name,
        "stage_name": deal.stage_name,
    }

    if getattr(deal, "pipeline_id", None):
        result["pipeline_id"] = str(deal.pipeline_id)
    if getattr(deal, "stage_id", None):
        result["stage_id"] = str(deal.stage_id)
    if getattr(deal, "owner_id", None):
        result["owner_user_id"] = str(deal.owner_id)
    owner_name = getattr(deal, "owner_name", None)
    if owner_name:
        result["owner_name"] = str(owner_name)
    deal_status = getattr(deal, "deal_status", None)
    if deal_status:
        result["deal_status"] = str(getattr(deal_status, "value", deal_status))
    if getattr(deal, "company_id", None):
        result["company_id"] = str(deal.company_id)
    message = f"Created deal **{deal.name}** (Deal ID: {deal_id})"
    if deal.pipeline_name and deal.stage_name:
        message += f" in {deal.pipeline_name} > {deal.stage_name}"
    elif deal.pipeline_name:
        message += f" in {deal.pipeline_name}"
    elif deal.stage_name:
        message += f" at stage {deal.stage_name}"

    if deal.amount is not None:
        result["amount"] = deal.amount
        result["currency"] = deal.currency
        currency = deal.currency or "USD"
        message += f". Amount: {currency} {deal.amount:,.2f}"

    if owner_name:
        message += f". Owner: {owner_name}"
    if deal_status:
        message += f". Status: {getattr(deal_status, 'value', deal_status)}"
    result["message"] = f"{message}."
    return result


def _public_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if not key.startswith("_")}


def _has_custom_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _custom_value_for_schema_key(custom_columns: dict[str, Any], key: str) -> Any:
    if key in custom_columns:
        return custom_columns[key]
    for raw_key, value in custom_columns.items():
        if DealCustomFieldWriteService.normalize_custom_key(raw_key) == key:
            return value
    return None


def _missing_required_custom_fields(
    fields: list[Any],
    custom_columns: dict[str, Any],
) -> list[dict[str, Any]]:
    missing: list[dict[str, Any]] = []
    for field in fields:
        validation = getattr(field, "internal_write_validation", None)
        if not isinstance(validation, dict) or validation.get("required") is not True:
            continue
        if getattr(field, "internal_write", False) is not True:
            continue
        write_path = getattr(field, "write_path", None)
        if not isinstance(write_path, str) or not write_path.startswith("custom_columns."):
            continue
        key = getattr(field, "key", "")
        if not isinstance(key, str) or _has_custom_value(
            _custom_value_for_schema_key(custom_columns, key)
        ):
            continue
        options = getattr(field, "options", {})
        missing.append(
            {
                "key": key,
                "label": getattr(field, "label", key),
                "type": getattr(field, "type", "text"),
                "write_path": write_path,
                "options": options if isinstance(options, dict) else {},
            }
        )
    return missing


async def _required_custom_field_guidance(
    *,
    session: Any,
    organisation_id: UUID,
    custom_columns: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        schema = await DealSchemaService(session, organisation_id).get_schema()
    except Exception:
        logger.debug("create_deal_direct: failed to load deal schema metadata", exc_info=True)
        return None

    missing = _missing_required_custom_fields(schema.fields, custom_columns)
    if not missing:
        return None

    labels = ", ".join(field["label"] for field in missing)
    return {
        "status": "needs_input",
        "message": f"Missing required custom fields: {labels}.",
        "schema_endpoint": "/users/crm/schema?entity=deal",
        "required_custom_fields": missing,
    }


def _escape_slack_mrkdwn(value: str) -> str:
    """Escape special characters for Slack mrkdwn."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_field(label: str, value: Any) -> dict[str, str]:
    """Build a Slack section field with label + value."""
    text = _escape_slack_mrkdwn(str(value))
    return {"type": "mrkdwn", "text": f"*{label}*\n{text}"}


def _slack_quote(value: str) -> str:
    """Render multi-line text as a Slack blockquote."""
    escaped = _escape_slack_mrkdwn(value)
    lines = escaped.splitlines() or [escaped]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def _deal_created_text_fallback(deal: Any) -> str:
    message = f"Created deal {deal.name} (Deal ID: {deal.deal_id})"
    if deal.pipeline_name and deal.stage_name:
        message += f" in {deal.pipeline_name} > {deal.stage_name}"
    elif deal.pipeline_name:
        message += f" in {deal.pipeline_name}"
    elif deal.stage_name:
        message += f" at stage {deal.stage_name}"
    if getattr(deal, "owner_name", None):
        message += f". Owner: {deal.owner_name}"
    if getattr(deal, "deal_status", None):
        status = getattr(deal.deal_status, "value", deal.deal_status)
        message += f". Status: {status}"
    return f"{message}."


def _success_card_facts(deal: Any) -> list[dict[str, str]]:
    facts = [
        {"title": "Name", "value": str(deal.name)},
        {"title": "Deal ID", "value": str(deal.deal_id)},
    ]
    if getattr(deal, "owner_name", None):
        facts.append({"title": "Owner", "value": str(deal.owner_name)})
    if getattr(deal, "deal_status", None):
        status = getattr(deal.deal_status, "value", deal.deal_status)
        facts.append({"title": "Status", "value": str(status)})
    if getattr(deal, "amount", None) is not None:
        currency = getattr(deal, "currency", None) or "USD"
        facts.append({"title": "Amount", "value": f"{currency} {deal.amount:,.2f}"})
    if getattr(deal, "pipeline_name", None):
        facts.append({"title": "Pipeline", "value": str(deal.pipeline_name)})
    if getattr(deal, "stage_name", None):
        facts.append({"title": "Stage", "value": str(deal.stage_name)})
    if getattr(deal, "company_name", None):
        facts.append({"title": "Account", "value": str(deal.company_name)})
    return facts


def _success_ui_card_facts(deal: Any) -> list[dict[str, str]]:
    return [{"label": fact["title"], "value": fact["value"]} for fact in _success_card_facts(deal)]


def _build_deal_created_ui_card(deal: Any) -> dict[str, Any]:
    return {
        "kind": "deal_created_confirmation",
        "version": "1.0",
        "tone": "success",
        "title": "Deal created",
        "message": _deal_created_text_fallback(deal),
        "entity": {
            "type": "deal",
            "id": str(deal.deal_id),
            "name": str(deal.name),
        },
        "facts": _success_ui_card_facts(deal),
        "actions": [],
    }


def _build_teams_success_payload(deal: Any) -> dict[str, Any]:
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": "Deal created",
                "size": "Medium",
                "weight": "Bolder",
                "wrap": True,
                "color": "Good",
            },
            {"type": "FactSet", "facts": _success_card_facts(deal)},
            {
                "type": "TextBlock",
                "text": "TODO: Add next-action buttons for add stakeholder, add note, and update stage.",
                "isVisible": False,
            },
        ],
    }
    return {
        "type": "message",
        "content": {
            "text": " ",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": card,
                }
            ],
        },
        "text_fallback": _deal_created_text_fallback(deal),
        "attachments": None,
        "source_text": None,
    }


def _build_slack_success_payload(deal: Any) -> dict[str, Any]:
    fields = [_slack_field(fact["title"], fact["value"]) for fact in _success_card_facts(deal)]
    return {
        "type": "message",
        "content": {
            "text": _deal_created_text_fallback(deal),
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": "Deal created"},
                },
                {"type": "section", "fields": fields},
            ],
        },
        "text_fallback": _deal_created_text_fallback(deal),
        "attachments": None,
        "source_text": None,
    }


def _build_whatsapp_success_payload(deal: Any) -> dict[str, Any]:
    lines = ["Deal created"]
    lines.extend(f"{fact['title']}: {fact['value']}" for fact in _success_card_facts(deal))
    return {
        "type": "message",
        "content": {"text": "\n".join(lines)},
        "text_fallback": _deal_created_text_fallback(deal),
        "attachments": None,
        "source_text": None,
    }


def _build_ui_card_success_payload(deal: Any) -> dict[str, Any]:
    return {
        "type": "ui_card",
        "content": {"card": _build_deal_created_ui_card(deal)},
        "text_fallback": _deal_created_text_fallback(deal),
        "attachments": None,
        "source_text": None,
    }


def _build_deal_created_channel_payload(context: AgentContext, deal: Any) -> dict[str, Any] | None:
    channel = (context.channel or "").lower()
    if channel == "slack":
        return _build_slack_success_payload(deal)
    if channel == "teams":
        return _build_teams_success_payload(deal)
    if channel == "whatsapp":
        return _build_whatsapp_success_payload(deal)
    if channel in {"web", "voice"}:
        return _build_ui_card_success_payload(deal)
    return None


async def create_deal_form(
    *,
    context: AgentContext,
    name: str | None = None,
    amount: float | None = None,
    currency: str | None = None,
    description: str | None = None,
    expected_close_date: str | None = None,
    priority: str | None = None,
    account_name: str | None = None,
    account_domain: str | None = None,
) -> dict[str, Any] | None:
    """Build and send a confirmation preview card for deal creation.

    Queries pipelines/stages for defaults, builds a read-only Adaptive Card,
    and sends it to the user's chat. The user clicks Confirm to create the deal.
    """
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("create_deal_form: missing organisation_id in context")
        return None

    if not name or not name.strip():
        return {
            "status": "error",
            "message": (
                "Deal name is required. Please ask the user for a deal name "
                "before calling this tool."
            ),
        }

    channel = (context.channel or "teams").lower()
    service_url = context.metadata.get("service_url", "")
    conversation_id = context.metadata.get("conversation_id", "")
    team_id = context.metadata.get("team_id", "")
    channel_id = context.metadata.get("channel_id", "")

    if channel == "slack":
        if not team_id or not channel_id:
            logger.warning("create_deal_form: missing Slack team_id/channel_id in metadata")
            return {
                "status": "error",
                "message": "Cannot send confirmation card: missing Slack channel context.",
            }
    elif not service_url or not conversation_id:
        logger.warning(
            "create_deal_form: missing Teams service_url/conversation_id in context metadata"
        )
        return {
            "status": "error",
            "message": "Cannot send confirmation card: missing Teams channel context.",
        }

    # Query pipelines and stages to resolve default names for the preview
    default_pipeline_name: str | None = None
    default_pipeline_id: str | None = None
    default_stage_name: str | None = None

    pipeline_c = cast(Any, CRMPipeline).__table__.c
    stage_c = cast(Any, CRMDealStage).__table__.c

    async with context.acquire_session() as session:
        try:
            pipeline_stmt = (
                select(
                    col(CRMPipeline.pipeline_id),
                    col(CRMPipeline.name),
                    col(CRMPipeline.is_default),
                )
                .where(
                    pipeline_c.organisation_id == organisation_id,
                    pipeline_c.is_deleted.is_(False),
                    pipeline_c.is_active.is_(True),
                )
                .order_by(
                    pipeline_c.display_order.asc().nulls_last(),
                    pipeline_c.name.asc(),
                )
            )
            pipeline_result = await session.execute(pipeline_stmt)
            pipeline_rows = pipeline_result.all()

            for p in pipeline_rows:
                if p.is_default or default_pipeline_id is None:
                    default_pipeline_id = str(p.pipeline_id)
                    default_pipeline_name = p.name

            # Preview cards need a concrete stage label even before the user
            # submits the form; use the first configured stage as the same
            # fallback the write service applies during creation.
            if default_pipeline_id:
                stage_stmt = (
                    select(
                        col(CRMDealStage.stage_id),
                        col(CRMDealStage.name),
                    )
                    .where(
                        stage_c.pipeline_id == UUID(default_pipeline_id),
                        stage_c.is_deleted.is_(False),
                    )
                    .order_by(
                        stage_c.display_order.asc().nulls_last(),
                        stage_c.name.asc(),
                    )
                    .limit(1)
                )
                stage_result = await session.execute(stage_stmt)
                first_stage = stage_result.first()
                if first_stage:
                    default_stage_name = first_stage.name
        except Exception as e:
            logger.exception("create_deal_form: error querying pipelines/stages: %s", e)

    # Build preview card
    nonce = create_nonce("deal_confirm")
    em = "\u2014"  # em dash for empty fields

    amount_display = em
    if amount is not None:
        curr = (currency or "USD").upper()
        amount_display = f"{curr} {amount:,.2f}"

    facts = [
        {"title": "Name", "value": name},
        {"title": "Amount", "value": amount_display},
        {"title": "Pipeline", "value": default_pipeline_name or em},
        {"title": "Stage", "value": default_stage_name or em},
        {"title": "Expected Close Date", "value": expected_close_date or em},
        {
            "title": "Priority",
            "value": (priority or em).capitalize() if priority else em,
        },
        {"title": "Account", "value": account_name or em},
        {"title": "Description", "value": description or em},
    ]

    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": "Create New Deal",
            "size": "Medium",
            "weight": "Bolder",
            "wrap": True,
        },
        {"type": "FactSet", "facts": facts},
        {
            "type": "TextBlock",
            "text": "Please confirm, edit, or cancel this deal creation.",
            "size": "Small",
            "isSubtle": True,
            "wrap": True,
            "spacing": "Medium",
        },
    ]

    # All deal data embedded in button payloads
    deal_data: dict[str, Any] = {
        "nonce": nonce,
        "organisation_id": str(organisation_id),
        "name": name,
        "amount": amount,
        "currency": (currency or "USD").upper(),
        "description": description or "",
        "expected_close_date": expected_close_date or "",
        "priority": priority or "",
        "account_name": account_name or "",
        "account_domain": account_domain or "",
        "pipeline_id": default_pipeline_id or "",
    }

    cancel_data: dict[str, Any] = {
        "nonce": nonce,
        "organisation_id": str(organisation_id),
    }

    confirm_fallback = {"action": "create_deal_submit", **deal_data}
    edit_fallback = {"action": "create_deal_edit", **deal_data}
    cancel_fallback = {"action": "create_deal_cancel", **cancel_data}

    actions = [
        {
            "type": "Action.Execute",
            "title": "Confirm",
            "style": "positive",
            "verb": "create_deal_submit",
            "data": deal_data,
            "fallback": {
                "type": "Action.Submit",
                "title": "Confirm",
                "style": "positive",
                "data": confirm_fallback,
            },
        },
        {
            "type": "Action.Execute",
            "title": "Edit",
            "verb": "create_deal_edit",
            "data": dict(deal_data),
            "fallback": {
                "type": "Action.Submit",
                "title": "Edit",
                "data": edit_fallback,
            },
        },
        {
            "type": "Action.Execute",
            "title": "Cancel",
            "style": "destructive",
            "verb": "create_deal_cancel",
            "data": cancel_data,
            "fallback": {
                "type": "Action.Submit",
                "title": "Cancel",
                "style": "destructive",
                "data": cancel_fallback,
            },
        },
    ]

    if channel == "slack":
        from src.service.bots.slack_bot_service import SlackBotService

        confirm_value = json.dumps(deal_data, separators=(",", ":"))
        cancel_value = json.dumps(cancel_data, separators=(",", ":"))
        description_value = description or em
        slack_fields = [
            _slack_field("Name", name),
            _slack_field("Amount", amount_display),
            _slack_field("Pipeline", default_pipeline_name or em),
            _slack_field("Stage", default_stage_name or em),
            _slack_field("Expected Close Date", expected_close_date or em),
            _slack_field("Priority", (priority or em).capitalize() if priority else em),
            _slack_field("Account", account_name or em),
            _slack_field("Domain", account_domain or em),
        ]
        slack_blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "Create New Deal"},
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Review the details below before creating the deal.",
                    }
                ],
            },
            {"type": "divider"},
            {
                "type": "section",
                "fields": slack_fields,
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Description*\n{_slack_quote(str(description_value))}",
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Click *Confirm* to create the deal or *Cancel* to discard.",
                    }
                ],
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Confirm"},
                        "style": "primary",
                        "action_id": "create_deal_submit",
                        "value": confirm_value,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Cancel"},
                        "style": "danger",
                        "action_id": "create_deal_cancel",
                        "value": cancel_value,
                    },
                ],
            },
        ]

        slack_bot_service = SlackBotService()
        sent = await slack_bot_service.send_block_kit_message(
            team_id=team_id,
            channel_id=channel_id,
            blocks=slack_blocks,
            text_fallback=f"Deal creation preview for {name}",
        )
    else:
        card_payload = {
            "text": " ",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": body,
                        "actions": actions,
                    },
                }
            ],
        }

        # Send the card
        teams_bot_service = TeamsBotService()
        sent = await teams_bot_service.send_proactive_payload(
            service_url=service_url,
            conversation_id=conversation_id,
            payload=card_payload,
            text_fallback="Deal creation preview",
        )

    if sent:
        return {
            "status": "preview_sent",
            "message": (
                "I've sent a deal creation preview to your chat. "
                "Please review the details and click **Confirm** to create it"
                + (", **Edit** to modify the details," if channel != "slack" else "")
                + " or **Cancel** to discard."
            ),
        }
    else:
        logger.error("create_deal_form: failed to send proactive payload")
        return {
            "status": "error",
            "message": "Failed to send the deal creation preview. Please try again.",
        }


async def create_deal_direct(
    *,
    context: AgentContext,
    name: str | None = None,
    amount: float | None = None,
    currency: str | None = None,
    description: str | None = None,
    expected_close_date: str | None = None,
    priority: str | None = None,
    account_name: str | None = None,
    account_domain: str | None = None,
    pipeline_id: UUID | None = None,
    stage_id: UUID | None = None,
    custom_columns: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a deal directly through the internal CRM write service."""
    organisation_id = context.organisation_id
    user_id = context.user_id
    deal_name = _clean_optional(name)

    if not organisation_id:
        logger.warning("create_deal_direct: missing organisation_id in context")
        return {"status": "error", "message": "Cannot create deal: missing organisation context."}
    if not user_id:
        logger.warning("create_deal_direct: missing user_id in context")
        return {"status": "error", "message": "Could not identify your account. Please sign in."}
    if not deal_name:
        return {
            "status": "error",
            "message": "Deal name is required. Please ask the user for a deal name.",
        }
    try:
        await ensure_crm_write_scope(organisation_id, "deals:create")
    except OrganisationPolicyDeniedError as exc:
        return {"status": "error", "message": exc.message}
    if custom_columns is not None and not isinstance(custom_columns, dict):
        return {
            "status": "error",
            "message": "custom_columns must be an object keyed by custom field key.",
        }

    payload: dict[str, Any] = {
        "name": deal_name,
        "owner_user_id": user_id,
        "created_via": "agent_create_deal",
    }
    custom_payload = custom_columns if isinstance(custom_columns, dict) else {}
    if custom_payload:
        payload["custom_properties"] = custom_payload
    if pipeline_id is not None:
        payload["pipeline_id"] = pipeline_id
    if stage_id is not None:
        payload["stage_id"] = stage_id

    if amount is not None:
        payload["amount"] = amount
        payload["currency"] = (_clean_optional(currency) or "USD").upper()

    optional_fields = {
        "description": _clean_optional(description),
        "expected_close_date": _parse_expected_close_date(expected_close_date),
        "priority": _clean_optional(priority),
    }
    for field, value in optional_fields.items():
        if value is not None:
            payload[field] = value

    clean_account_name = _clean_optional(account_name)
    clean_account_domain = _clean_optional(account_domain)
    if clean_account_name or clean_account_domain:
        payload["account"] = {
            "name": clean_account_name,
            "domain": clean_account_domain,
        }

    async for master_session in acquire_master_async_session():
        async with context.acquire_session() as org_session:
            guidance = await _required_custom_field_guidance(
                session=org_session,
                organisation_id=organisation_id,
                custom_columns=custom_payload,
            )
            if guidance is not None:
                return guidance

            service = DealWriteService(
                org_session=org_session,
                master_session=master_session,
                organisation_id=organisation_id,
                actor_user_id=user_id,
            )
            try:
                deal = await service.create_deal(payload)
                await org_session.commit()
                result = _created_deal_result(deal)
                channel_payload = _build_deal_created_channel_payload(context, deal)
                if channel_payload:
                    if channel_payload.get("type") == "ui_card":
                        result["ui_card"] = channel_payload["content"]["card"]
                    result[_BOT_CHANNEL_PAYLOAD_KEY] = channel_payload
                return result
            except DealWriteServiceError as exc:
                await org_session.rollback()
                logger.warning("create_deal_direct: DealWriteServiceError: %s", exc.message)
                return {
                    "status": "error",
                    "message": f"Could not create deal: {exc.message}",
                }
            except Exception:
                await org_session.rollback()
                logger.exception("create_deal_direct: unexpected error")
                return {
                    "status": "error",
                    "message": "An unexpected error occurred while creating the deal.",
                }

    return {"status": "error", "message": "Could not acquire database session."}


def create_create_deal(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the create_deal tool bound to a context.

    SECURITY: organisation_id comes from context (immutable, not a tool parameter).
    """

    async def create_deal_tool(
        name: str,
        amount: float | None = None,
        currency: str | None = None,
        description: str | None = None,
        expected_close_date: str | None = None,
        priority: str | None = None,
        account_name: str | None = None,
        account_domain: str | None = None,
        pipeline_id: UUID | None = None,
        stage_id: UUID | None = None,
        custom_columns: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Create a new deal directly once the deal name is known.

        Use this tool when the user wants to create a new deal. Extract deal
        details from the conversation and pass them as values. If the user names
        a pipeline or stage, use the pipeline tools to obtain its UUID and pass
        it here. If only the deal name is known, still call this tool immediately.
        Do not ask follow-up questions for optional fields like account/company
        before creating it.
        If the result status is "needs_input", ask for the returned
        required_custom_fields and call the tool again with custom_columns.

        name is REQUIRED. If the user says "create a deal" without providing a
        name, ask them for the deal name first before calling this tool.

        Args:
            name: Deal name (REQUIRED, e.g. "Acme Corp - Enterprise License")
            amount: Deal amount to pre-fill (e.g. 50000)
            currency: Currency code to pre-fill (e.g. "USD", "EUR"). Defaults to USD.
            description: Deal description to pre-fill
            expected_close_date: Expected close date to pre-fill (YYYY-MM-DD format)
            priority: Priority level to pre-fill ("low", "medium", "high")
            account_name: Account/company name to pre-fill
            account_domain: Account website domain to pre-fill (e.g. "acme.com")
            pipeline_id: Pipeline UUID from the pipeline tools; omit to use the default
            stage_id: Stage UUID from the pipeline tools; omit to use the first stage
            custom_columns: Custom field values keyed by schema field key

        Returns:
            Structured status dict with created deal details or an error message.
        """
        logger.info(
            "[TOOL CALLED] create_deal - name='%s', amount=%s, currency='%s'",
            name,
            amount,
            currency,
        )

        if not context.organisation_id:
            logger.warning("create_deal unavailable: missing organisation_id")
            return None

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="CreateDeal",
            tool_func=create_deal_direct,
            parameters={
                "context": context,
                "name": name,
                "amount": amount,
                "currency": currency,
                "description": description,
                "expected_close_date": expected_close_date,
                "priority": priority,
                "account_name": account_name,
                "account_domain": account_domain,
                "pipeline_id": pipeline_id,
                "stage_id": stage_id,
                "custom_columns": custom_columns,
            },
            description="Creating deal",
            fallback_value=None,
        )

        if result:
            logger.info(
                "[TOOL RESULT] create_deal status=%s",
                result.get("status"),
            )
        else:
            logger.info("[TOOL RESULT] create_deal returned None (error)")

        return _public_tool_result(dict(result)) if result else None

    return create_deal_tool


__all__ = [
    "create_deal_direct",
    "create_deal_form",
    "create_create_deal",
]
