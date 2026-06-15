"""Agent tool used by chat workflows to add CRM stakeholders.

The direct path creates the stakeholder immediately once a resolved `deal_id` and
email are available. The form path is used for Teams/Slack interactive cards
and delegates submission handling to existing form handlers.

The tool is invoked with an immutable `organisation_id` context from the agent
runtime, which prevents cross-org writes and keeps stakeholder mutation logic in
the CRM service boundary.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access

This is an ORG-SCOPED tool. deal_id is NOT required at init.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.adapters.card_builders import FormDefinition, FormField, get_card_builder
from src.agents.tools.base import execute_with_capabilities
from src.contracts.crm.stakeholders import CreateStakeholderRequest
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    DecisionPower,
    StakeholderStance,
)
from src.service.crm.guards.internal_user_guard import is_internal_email
from src.service.crm.insights.stakeholders_service import (
    StakeholdersService,
    StakeholdersServiceError,
)
from src.service.crm.jobs.stakeholder_job_visibility_service import (
    StakeholderJobVisibilityService,
)
from src.workers.deal_worker.utils.stakeholder_research_dispatch import (
    dispatch_stakeholder_research_async,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

_BOT_CHANNEL_PAYLOAD_KEY = "_bot_channel_payload"

STANCE_CHOICES: list[dict[str, str]] = [
    {"title": "Unknown", "value": "UNKNOWN"},
    {"title": "Champion", "value": "CHAMPION"},
    {"title": "Supporter", "value": "SUPPORTER"},
    {"title": "Neutral", "value": "NEUTRAL"},
    {"title": "Detractor", "value": "DETRACTOR"},
    {"title": "Blocker", "value": "BLOCKER"},
]

DECISION_POWER_CHOICES: list[dict[str, str]] = [
    {"title": "Unknown", "value": "UNKNOWN"},
    {"title": "Decision Maker", "value": "DECISION_MAKER"},
    {"title": "Influencer", "value": "INFLUENCER"},
    {"title": "Evaluator", "value": "EVALUATOR"},
    {"title": "Gatekeeper", "value": "GATEKEEPER"},
    {"title": "End User", "value": "END_USER"},
]


def _clean_optional(value: str | None) -> str | None:
    """Normalize optional user input while preserving explicit empty submissions."""
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _public_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    """Filter internal metadata from tool payload before returning to chat.

    The caller may only see a subset of keys; internal transport payloads like
    `_bot_channel_payload` are intentionally hidden from UI-facing handlers.
    """
    return {key: value for key, value in result.items() if not key.startswith("_")}


def _enum_value(value: Any) -> str | None:
    """Convert enum-like values to string in a safe, API-friendly format."""
    if value is None:
        return None
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _parse_stance(value: str | None) -> StakeholderStance | None | dict[str, Any]:
    cleaned = _clean_optional(value)
    if cleaned is None:
        return None
    try:
        return StakeholderStance(cleaned.upper())
    except ValueError:
        allowed = ", ".join(item.value for item in StakeholderStance)
        return {
            "status": "error",
            "message": f"Invalid stance '{cleaned}'. Use one of: {allowed}.",
        }


def _parse_decision_power(value: str | None) -> DecisionPower | None | dict[str, Any]:
    cleaned = _clean_optional(value)
    if cleaned is None:
        return None
    try:
        return DecisionPower(cleaned.upper())
    except ValueError:
        allowed = ", ".join(item.value for item in DecisionPower)
        return {
            "status": "error",
            "message": f"Invalid decision_power '{cleaned}'. Use one of: {allowed}.",
        }


def _job_summary(job: Any) -> dict[str, str]:
    return {
        "job_id": str(job.job_id),
        "job_type": _enum_value(job.job_type) or "",
        "status": _enum_value(job.status) or "",
    }


def _research_job_summary(job_id: UUID | str | None) -> dict[str, str | None] | None:
    if not job_id:
        return None
    return {
        "job_id": str(job_id),
        "job_type": "stakeholder_research",
        "status": "pending",
    }


def _post_create_status(
    *,
    queued: bool,
    error: str | None = None,
) -> dict[str, str]:
    status = "failed" if error else "queued" if queued else "not_created"
    summary = {"status": status}
    if error:
        summary["error"] = error
    return summary


def _stakeholder_name(stakeholder: Any) -> str:
    return str(stakeholder.name or stakeholder.email)


def _stakeholder_added_text_fallback(stakeholder: Any, deal_name: str) -> str:
    return f"Added {_stakeholder_name(stakeholder)} as a stakeholder to {deal_name}."


def _success_card_facts(stakeholder: Any, deal_name: str) -> list[dict[str, str]]:
    facts = [
        {"title": "Deal", "value": deal_name},
        {"title": "Email", "value": str(stakeholder.email)},
    ]
    if getattr(stakeholder, "name", None):
        facts.append({"title": "Name", "value": str(stakeholder.name)})
    if getattr(stakeholder, "job_title", None):
        facts.append({"title": "Title", "value": str(stakeholder.job_title)})
    if _enum_value(getattr(stakeholder, "stance", None)):
        facts.append({"title": "Stance", "value": _enum_value(stakeholder.stance) or ""})
    if _enum_value(getattr(stakeholder, "decision_power", None)):
        facts.append(
            {"title": "Decision Power", "value": _enum_value(stakeholder.decision_power) or ""}
        )
    return facts


def _escape_slack_mrkdwn(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_field(label: str, value: Any) -> dict[str, str]:
    return {"type": "mrkdwn", "text": f"*{label}*\n{_escape_slack_mrkdwn(str(value))}"}


def _build_teams_success_payload(stakeholder: Any, deal_name: str) -> dict[str, Any]:
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": "Stakeholder added",
                "size": "Medium",
                "weight": "Bolder",
                "wrap": True,
                "color": "Good",
            },
            {"type": "FactSet", "facts": _success_card_facts(stakeholder, deal_name)},
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
        "text_fallback": _stakeholder_added_text_fallback(stakeholder, deal_name),
        "attachments": None,
        "source_text": None,
    }


def _build_slack_success_payload(stakeholder: Any, deal_name: str) -> dict[str, Any]:
    fields = [
        _slack_field(fact["title"], fact["value"])
        for fact in _success_card_facts(stakeholder, deal_name)
    ]
    return {
        "type": "message",
        "content": {
            "text": _stakeholder_added_text_fallback(stakeholder, deal_name),
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": "Stakeholder added"},
                },
                {"type": "section", "fields": fields},
            ],
        },
        "text_fallback": _stakeholder_added_text_fallback(stakeholder, deal_name),
        "attachments": None,
        "source_text": None,
    }


def _build_stakeholder_added_channel_payload(
    context: AgentContext,
    stakeholder: Any,
    deal_name: str,
) -> dict[str, Any] | None:
    channel = (context.channel or "").lower()
    if channel == "teams":
        return _build_teams_success_payload(stakeholder, deal_name)
    if channel == "slack":
        return _build_slack_success_payload(stakeholder, deal_name)
    return None


def _created_stakeholder_result(
    *,
    context: AgentContext,
    deal_id: UUID,
    deal_name: str,
    stakeholder: Any,
    processing_jobs: list[Any],
    research_job_id: UUID | str | None,
    processing_error: str | None = None,
    research_error: str | None = None,
) -> dict[str, Any]:
    processing_job_summaries = [_job_summary(job) for job in processing_jobs]
    result: dict[str, Any] = {
        "status": "created",
        "participant_id": str(stakeholder.participant_id),
        "deal_id": str(deal_id),
        "deal_name": deal_name,
        "email": stakeholder.email,
        "name": stakeholder.name,
        "job_title": getattr(stakeholder, "job_title", None),
        "phone_number": getattr(stakeholder, "phone_number", None),
        "stance": _enum_value(getattr(stakeholder, "stance", None)),
        "decision_power": _enum_value(getattr(stakeholder, "decision_power", None)),
        "approval_status": _enum_value(getattr(stakeholder, "approval_status", None)),
        "processing_jobs": processing_job_summaries,
        "research_job": _research_job_summary(research_job_id),
        "post_create_jobs": {
            "processing": _post_create_status(
                queued=bool(processing_jobs),
                error=processing_error,
            ),
            "research": _post_create_status(
                queued=research_job_id is not None,
                error=research_error,
            ),
        },
        "message": (
            f"Added **{_stakeholder_name(stakeholder)}** as a stakeholder to **{deal_name}**."
        ),
    }

    channel_payload = _build_stakeholder_added_channel_payload(context, stakeholder, deal_name)
    if channel_payload:
        result[_BOT_CHANNEL_PAYLOAD_KEY] = channel_payload
    return result


async def add_stakeholder_direct(
    *,
    context: AgentContext,
    deal_id: str,
    deal_name: str,
    email: str | None = None,
    name: str | None = None,
    job_title: str | None = None,
    phone_number: str | None = None,
    department: str | None = None,
    stance: str | None = None,
    decision_power: str | None = None,
    motivation: str | None = None,
    pain_points: list[str] | None = None,
) -> dict[str, Any]:
    """Create a stakeholder directly through the CRM stakeholder service."""
    # Security boundary check is deliberately early: the tool cannot execute for
    # missing org/user context because those IDs are the only safe tenant filter.
    organisation_id = context.organisation_id
    user_id = context.user_id
    clean_deal_name = _clean_optional(deal_name)
    clean_email = _clean_optional(email)

    if not organisation_id:
        logger.warning("add_stakeholder_direct: missing organisation_id in context")
        return {
            "status": "error",
            "message": "Cannot add stakeholder: missing organisation context.",
        }
    if not user_id:
        logger.warning("add_stakeholder_direct: missing user_id in context")
        return {"status": "error", "message": "Could not identify your account. Please sign in."}
    if not clean_deal_name:
        return {"status": "error", "message": "Deal name is required before adding a stakeholder."}
    if not clean_email:
        return {
            "status": "error",
            "message": "Stakeholder email is required. Please ask the user for the email.",
        }

    try:
        deal_uuid = UUID(deal_id)
    except (TypeError, ValueError):
        return {
            "status": "error",
            "message": "Invalid deal information. Please search the deal again.",
        }

    parsed_stance = _parse_stance(stance)
    if isinstance(parsed_stance, dict):
        return parsed_stance
    parsed_decision_power = _parse_decision_power(decision_power)
    if isinstance(parsed_decision_power, dict):
        return parsed_decision_power

    try:
        request = CreateStakeholderRequest(
            email=clean_email,
            name=_clean_optional(name),
            job_title=_clean_optional(job_title),
            phone_number=_clean_optional(phone_number),
            department=_clean_optional(department),
            stance=parsed_stance or StakeholderStance.UNKNOWN,
            decision_power=parsed_decision_power or DecisionPower.UNKNOWN,
            motivation=_clean_optional(motivation),
            pain_points=pain_points,
            external_links=None,
        )
    except ValueError as exc:
        return {"status": "error", "message": f"Invalid stakeholder details: {exc}"}

    # Keep DB session scope tight to this path; all writes stay in-service and
    # can fail independently from the outbound research dispatch.
    async with context.acquire_session() as session:
        service = StakeholdersService(session, organisation_id, user_id)
        try:
            stakeholder = await service.create_stakeholder(deal_uuid, request)
            processing_jobs: list[Any] = []
            processing_error = None
            research_job_id = None
            research_error = None

            try:
                job_visibility_service = StakeholderJobVisibilityService(
                    session,
                    organisation_id,
                    user_id,
                )
                processing_jobs = await job_visibility_service.create_stakeholder_processing_jobs(
                    deal_uuid,
                    stakeholder.participant_id,
                )
            except Exception as exc:
                processing_error = str(exc)
                logger.exception(
                    "add_stakeholder_direct: stakeholder created but processing dispatch failed"
                )

            try:
                research_job_id = await dispatch_stakeholder_research_async(
                    organisation_id=organisation_id,
                    stakeholder_id=stakeholder.participant_id,
                    deal_id=deal_uuid,
                    user_id=user_id,
                )
            except Exception as exc:
                research_error = str(exc)
                logger.exception(
                    "add_stakeholder_direct: stakeholder created but research dispatch failed"
                )

            return _created_stakeholder_result(
                context=context,
                deal_id=deal_uuid,
                deal_name=clean_deal_name,
                stakeholder=stakeholder,
                processing_jobs=processing_jobs,
                research_job_id=research_job_id,
                processing_error=processing_error,
                research_error=research_error,
            )
        except StakeholdersServiceError as exc:
            logger.warning("add_stakeholder_direct: StakeholdersServiceError: %s", exc.message)
            result: dict[str, Any] = {
                "status": "error",
                "message": f"Could not add stakeholder: {exc.message}",
            }
            if exc.data:
                result["conflict"] = exc.data
            return result
        except Exception:
            logger.exception("add_stakeholder_direct: unexpected error")
            return {
                "status": "error",
                "message": "An unexpected error occurred while adding the stakeholder.",
            }


async def add_stakeholder_form(
    *,
    context: AgentContext,
    deal_id: str,
    deal_name: str,
    email: str | None = None,
    name: str | None = None,
    job_title: str | None = None,
    phone_number: str | None = None,
    stance: str | None = None,
    decision_power: str | None = None,
) -> dict[str, Any] | None:
    """Send an interactive stakeholder form for a known deal to the user's chat.

    Args:
        context: AgentContext with organisation_id and channel metadata
        deal_id: UUID string of the deal (already resolved by LLM)
        deal_name: Display name of the deal (already resolved by LLM)
        email: Optional pre-filled email address
        name: Optional pre-filled stakeholder name
        job_title: Optional pre-filled job title
        stance: Optional pre-filled stance value (e.g. "CHAMPION")
        decision_power: Optional pre-filled decision power (e.g. "INFLUENCER")

    Returns:
        Dict with status and message, or None on error
    """
    # Channel-specific interactive forms are only valid for Teams/Slack payloads
    # and therefore enforce channel metadata before any card is built.
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("add_stakeholder_form: missing organisation_id in context")
        return None

    # Guardrail: reject internal team members before sending the form
    if email and await is_internal_email(context, email):
        logger.info(
            "add_stakeholder_form: blocked internal email=%s org=%s",
            email,
            organisation_id,
        )
        return {
            "status": "rejected_internal_user",
            "message": (
                "This email belongs to an internal team member and cannot be added "
                "as a stakeholder. Stakeholders must be external contacts only "
                "(clients, prospects, partners)."
            ),
        }

    channel = (context.channel or "teams").lower()
    service_url = context.metadata.get("service_url", "")
    conversation_id = context.metadata.get("conversation_id", "")
    team_id = context.metadata.get("team_id", "")
    channel_id = context.metadata.get("channel_id", "")

    if channel == "slack":
        # Slack cards require team/channel identity to route the outgoing message.
        if not team_id or not channel_id:
            logger.warning("add_stakeholder_form: missing Slack team_id/channel_id in metadata")
            return {
                "status": "error",
                "message": "Cannot send interactive form: missing Slack channel context.",
            }
    elif not service_url or not conversation_id:
        logger.warning(
            "add_stakeholder_form: missing Teams service_url/conversation_id in metadata"
        )
        return {
            "status": "error",
            "message": "Cannot send interactive form: missing Teams channel context.",
        }

    # Build the form definition
    fields: list[FormField] = [
        FormField(
            id="email",
            label="Email",
            field_type="text",
            required=True,
            value=email,
            placeholder="e.g. john@acme.com",
        ),
        FormField(
            id="name",
            label="Name",
            field_type="text",
            required=False,
            value=name,
            placeholder="e.g. John Smith",
        ),
        FormField(
            id="job_title",
            label="Job Title",
            field_type="text",
            required=False,
            value=job_title,
            placeholder="e.g. VP of Sales",
        ),
        FormField(
            id="phone_number",
            label="Phone Number",
            field_type="text",
            required=False,
            value=phone_number,
            placeholder="e.g. +1 (555) 123-4567",
        ),
        FormField(
            id="stance",
            label="Stance",
            field_type="choice",
            required=False,
            value=stance,
            choices=STANCE_CHOICES,
        ),
        FormField(
            id="decision_power",
            label="Decision Power",
            field_type="choice",
            required=False,
            value=decision_power,
            choices=DECISION_POWER_CHOICES,
        ),
    ]

    form = FormDefinition(
        title="Add Stakeholder",
        subtitle=f"Deal: {deal_name}",
        fields=fields,
        submit_action="add_stakeholder_submit",
        submit_label="Add Stakeholder",
        cancel_action="add_stakeholder_cancel",
        cancel_label="Cancel",
        metadata={
            "organisation_id": str(organisation_id),
            "deal_id": deal_id,
            "deal_name": deal_name,
        },
    )

    # Build the card for the target channel
    try:
        builder = get_card_builder(context.channel)
        card_payload = builder.build(form)
    except ValueError:
        logger.warning("add_stakeholder_form: no card builder for channel=%s", context.channel)
        return {
            "status": "error",
            "message": f"Interactive forms are not supported on {context.channel}.",
        }

    # Send the card via the channel-specific bot service.
    if channel == "slack":
        from src.service.bots.slack_bot_service import SlackBotService

        slack_bot_service = SlackBotService()
        blocks = card_payload.get("blocks")
        text_fallback = card_payload.get("text") or "Add stakeholder to deal form"
        if blocks:
            sent = await slack_bot_service.send_block_kit_message(
                team_id=team_id,
                channel_id=channel_id,
                blocks=blocks,
                text_fallback=text_fallback,
            )
        else:
            sent = await slack_bot_service._send_message(team_id, channel_id, text_fallback)
    else:
        from src.service.bots.teams_bot_service import TeamsBotService

        teams_bot_service = TeamsBotService()
        sent = await teams_bot_service.send_proactive_payload(
            service_url=service_url,
            conversation_id=conversation_id,
            payload=card_payload,
            text_fallback="Add stakeholder to deal form",
        )

    if sent:
        return {
            "status": "form_sent",
            "message": (
                f"I've sent a form to add a stakeholder to deal **{deal_name}**. "
                "Please fill it out and submit."
            ),
        }
    else:
        logger.error("add_stakeholder_form: failed to send proactive payload")
        return {
            "status": "error",
            "message": "Failed to send the stakeholder form. Please try again.",
        }


def create_add_stakeholder(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the add_stakeholder tool bound to a context.

    SECURITY: organisation_id comes from context (immutable, not a tool parameter).
    """

    async def add_stakeholder_tool(
        deal_id: str,
        deal_name: str,
        email: str | None = None,
        name: str | None = None,
        job_title: str | None = None,
        phone_number: str | None = None,
        department: str | None = None,
        stance: str | None = None,
        decision_power: str | None = None,
        motivation: str | None = None,
        pain_points: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Create a stakeholder directly on an existing deal.

        IMPORTANT: Only add EXTERNAL contacts (clients, prospects, partners) as
        stakeholders. Do NOT add internal team members or anyone from your own
        organisation. Internal emails are rejected automatically by the service.

        IMPORTANT: Before calling this tool, you MUST first use search_deals to
        find the deal. Do NOT call this tool without a resolved deal_id.

        email is REQUIRED. If the user asks to add a stakeholder but does not
        provide an email address, ask for the email before calling this tool.

        Workflow:
        1. User asks to add a stakeholder to a deal
        2. You call search_deals to find matching deals
        3. If multiple matches, ask user which deal they mean
        4. Once the deal and email are known, call this tool with any optional
           stakeholder details the user mentioned

        Args:
            deal_id: UUID of the deal (from search_deals result)
            deal_name: Name of the deal (from search_deals result)
            email: Stakeholder email address (REQUIRED)
            name: Optional stakeholder name
            job_title: Optional job title
            phone_number: Optional phone number
            department: Optional department
            stance: Optional stance (UNKNOWN/CHAMPION/SUPPORTER/
                    NEUTRAL/DETRACTOR/BLOCKER)
            decision_power: Optional decision power (UNKNOWN/
                           DECISION_MAKER/INFLUENCER/EVALUATOR/GATEKEEPER/END_USER)
            motivation: Optional motivation/context
            pain_points: Optional list of pain points

        Returns:
            Structured status dict with created stakeholder details or an error.
        """
        logger.info(
            "[TOOL CALLED] add_stakeholder - deal_id='%s', deal_name='%s', email=%s",
            deal_id,
            deal_name,
            email,
        )

        if not context.organisation_id:
            logger.warning("add_stakeholder unavailable: missing organisation_id")
            return None

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="AddStakeholder",
            tool_func=add_stakeholder_direct,
            parameters={
                "context": context,
                "deal_id": deal_id,
                "deal_name": deal_name,
                "email": email,
                "name": name,
                "job_title": job_title,
                "phone_number": phone_number,
                "department": department,
                "stance": stance,
                "decision_power": decision_power,
                "motivation": motivation,
                "pain_points": pain_points,
            },
            description="Adding stakeholder",
            fallback_value=None,
        )

        if result:
            logger.info(
                "[TOOL RESULT] add_stakeholder status=%s",
                result.get("status"),
            )
        else:
            logger.info("[TOOL RESULT] add_stakeholder returned None (error)")

        return _public_tool_result(dict(result)) if result else None

    return add_stakeholder_tool


__all__ = [
    "add_stakeholder_direct",
    "add_stakeholder_form",
    "create_add_stakeholder",
]
