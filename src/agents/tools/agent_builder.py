"""GeneralAgent control-plane tools for private agents and personal triggers.

The web GeneralAgent calls these factories with an immutable organisation/user
context. Model-authored requests deliberately omit tenant identity, ownership,
visibility, and automation authority so those boundaries remain server-owned.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.agents.tools.base import execute_with_capabilities
from src.contracts.agent_models import AgentCreateRequest
from src.contracts.automation_trigger_models import (
    AutomationTriggerCreateRequest,
    AutomationTriggerOptionSearchRequest,
)
from src.domain.models.organisation_tables import AgentStatus
from src.service.agents.capability_catalog import build_agent_capability_catalog
from src.service.agents.definition_service import AgentDefinitionService
from src.service.agents.errors import AgentServiceError
from src.service.agents.template_metadata import agent_template_summary, list_agent_templates
from src.service.automation.errors import AutomationServiceError
from src.service.automation.triggers.capabilities import RETRY_UNSAFE_AUTOMATION_TOOLS
from src.service.automation.triggers.service import AutomationTriggerService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


class AgentBuilderOverviewRequest(BaseModel):
    """Request the current user's agents, templates, and selectable capabilities."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["overview"]
    status: Literal["active", "paused"] | None = None
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class AgentBuilderTriggerCatalogRequest(BaseModel):
    """Request live trigger definitions compatible with personal execution."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["trigger_catalog"]


class AgentBuilderTriggerOptionsRequest(BaseModel):
    """Search business labels for one trigger field or source selector."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["trigger_options"]
    trigger_key: str = Field(min_length=1, max_length=160)
    control: Literal["source_selector", "filter", "direct_subject"]
    field_path: str | None = Field(default=None, max_length=200)
    query: str = Field(default="", max_length=200)
    limit: int = Field(default=25, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=32)


class AgentBuilderCreateRequest(BaseModel):
    """Define the user-editable fields for one new private agent."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["create_agent"]
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2_000)
    prompt: str = Field(min_length=1, max_length=50_000)
    allowed_tools: list[str] = Field(default_factory=list, max_length=128)
    allowed_skills: list[str] = Field(default_factory=list, max_length=64)
    max_turns: int | None = Field(default=None, ge=1, le=100)
    input_schema: dict[str, Any] | None = None
    output_mode: Literal["text", "json"] = "text"
    output_schema: dict[str, Any] | None = None


class AgentBuilderUseTemplateRequest(BaseModel):
    """Copy one exact code-owned template version for the current user."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["use_template"]
    template_id: UUID
    version: int = Field(ge=1)
    confirm_duplicate: bool = False


class AgentBuilderAttachTriggerRequest(BaseModel):
    """Describe a personal trigger without accepting authority-bearing fields."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["attach_trigger"]
    agent_id: UUID
    name: str = Field(min_length=1, max_length=120)
    trigger_key: str = Field(min_length=3, max_length=160)
    status: Literal["active", "paused"]
    config: dict[str, Any] = Field(default_factory=dict, max_length=64)
    watch: dict[str, Any] | None = None
    filter: dict[str, Any] | None = Field(default=None, max_length=64)
    goal: str | None = Field(default=None, max_length=4_000)


AgentBuilderInspectRequest = Annotated[
    AgentBuilderOverviewRequest
    | AgentBuilderTriggerCatalogRequest
    | AgentBuilderTriggerOptionsRequest,
    Field(discriminator="action"),
]
AgentBuilderChangeRequest = Annotated[
    AgentBuilderCreateRequest | AgentBuilderUseTemplateRequest | AgentBuilderAttachTriggerRequest,
    Field(discriminator="action"),
]


def _service_error_result(exc: AgentServiceError | AutomationServiceError) -> dict[str, Any]:
    """Map expected service failures without exposing implementation details."""

    if exc.status_code == 409:
        status = "conflict"
    elif exc.status_code in {400, 404, 422}:
        status = "needs_input"
    else:
        status = "error"
    return {"status": status, "message": exc.message}


def _enum_value(value: Any) -> Any:
    """Return the JSON-facing value for enum-backed ORM fields."""

    return getattr(value, "value", value)


def _filter_personal_trigger_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    """Retain personal trigger definitions and annotate chat-only limitations."""

    categories = []
    for category in catalog.get("categories", []):
        resources = []
        for resource in category.get("resources", []):
            triggers = []
            for trigger in resource.get("triggers", []):
                if "personal" not in trigger.get("principal_modes", []):
                    continue
                trigger = dict(trigger)
                trigger["chat_supported"] = trigger.get("family") != "webhook"
                if not trigger["chat_supported"]:
                    trigger["chat_unsupported_reason"] = (
                        "Create webhooks in the Agents UI so one-time credentials stay "
                        "outside chat and tool-result history."
                    )
                triggers.append(trigger)
            if triggers:
                resources.append({**resource, "triggers": triggers})
        if resources:
            categories.append({**category, "resources": resources})
    return {**catalog, "categories": categories}


async def _inspect_agent_builder(
    context: AgentContext,
    request: AgentBuilderInspectRequest,
) -> dict[str, Any]:
    """Execute one read action inside a fresh tenant-scoped session."""

    if context.user_id is None:
        return {
            "status": "error",
            "message": "Agent management requires an authenticated user.",
        }

    try:
        # The session is held only for the catalog/service read and is released
        # before the model reasons over the result.
        async with context.acquire_session() as session:
            if isinstance(request, AgentBuilderOverviewRequest):
                service = AgentDefinitionService(
                    session,
                    context.organisation_id,
                    context.user_id,
                )
                status = AgentStatus(request.status) if request.status else None
                agents = await service.list_agents(
                    status=status,
                    limit=request.limit,
                    offset=request.offset,
                )
                templates = list_agent_templates()
                copies = await service.access.list_template_copies(
                    {template.template_id for template in templates}
                )
                copies_by_template: dict[UUID, list[dict[str, Any]]] = {}
                for copy in copies:
                    if copy.source_agent_id is None:
                        continue
                    copies_by_template.setdefault(copy.source_agent_id, []).append(
                        {
                            "agent_id": str(copy.agent_id),
                            "name": copy.name,
                            "status": _enum_value(copy.status),
                            "revision": copy.revision,
                            "source_version": copy.source_version,
                        }
                    )

                template_results = []
                for template in templates:
                    summary = agent_template_summary(template).model_dump(mode="json")
                    summary["existing_agents"] = copies_by_template.get(
                        template.template_id,
                        [],
                    )
                    template_results.append(summary)

                capabilities = build_agent_capability_catalog().model_dump(mode="json")
                for tool in capabilities["tools"]:
                    tool["automation_compatible"] = tool["key"] not in RETRY_UNSAFE_AUTOMATION_TOOLS
                return {
                    "status": "ok",
                    "agents": agents.model_dump(mode="json"),
                    "templates": template_results,
                    "capabilities": capabilities,
                }

            trigger_service = AutomationTriggerService(
                session,
                context.organisation_id,
                context.user_id,
                is_admin=False,
            )
            if isinstance(request, AgentBuilderTriggerCatalogRequest):
                catalog = await trigger_service.get_catalog()
                return {
                    "status": "ok",
                    "catalog": _filter_personal_trigger_catalog(catalog.model_dump(mode="json")),
                }

            options = await trigger_service.search_trigger_options(
                AutomationTriggerOptionSearchRequest(
                    trigger_key=request.trigger_key,
                    control=request.control,
                    field_path=request.field_path,
                    principal_mode="personal",
                    query=request.query,
                    limit=request.limit,
                    cursor=request.cursor,
                )
            )
            return {"status": "ok", "options": options.model_dump(mode="json")}
    except (AgentServiceError, AutomationServiceError) as exc:
        return _service_error_result(exc)
    except Exception:
        logger.exception("Agent builder inspection failed action=%s", request.action)
        return {
            "status": "error",
            "message": "Agent management inspection failed unexpectedly.",
        }


def _created_agent_result(detail: Any) -> dict[str, Any]:
    """Return only fields needed to continue the conversational workflow."""

    data = detail.model_dump(mode="json")
    return {
        "status": "created",
        "agent_id": data["agent_id"],
        "name": data["name"],
        "source_type": data["source_type"],
        "revision": data["revision"],
        "allowed_tools": data["allowed_tools"],
        "allowed_skills": data["allowed_skills"],
        "automation_eligibility": data["automation_eligibility"],
    }


def _template_copy_result(copy: Any) -> dict[str, Any]:
    """Serialize a prior owned copy without leaking owner or tenant identities."""

    return {
        "agent_id": str(copy.agent_id),
        "name": copy.name,
        "status": _enum_value(copy.status),
        "revision": copy.revision,
        "source_version": copy.source_version,
    }


def _trigger_family(catalog: dict[str, Any], trigger_key: str) -> str | None:
    """Resolve one family from the live registry-backed catalog."""

    for category in catalog.get("categories", []):
        for resource in category.get("resources", []):
            for trigger in resource.get("triggers", []):
                if trigger.get("trigger_key") == trigger_key:
                    family = trigger.get("family")
                    return family if isinstance(family, str) else None
    return None


async def _dispatch_agent_builder_change(
    session: Any,
    context: AgentContext,
    request: AgentBuilderChangeRequest,
) -> dict[str, Any]:
    """Delegate one model-authored change to the existing domain services."""

    user_id = context.user_id
    if user_id is None:
        raise RuntimeError("Authenticated user was not checked before mutation dispatch")

    if isinstance(request, AgentBuilderCreateRequest):
        definition_service = AgentDefinitionService(
            session,
            context.organisation_id,
            user_id,
        )
        detail = await definition_service.create_agent(
            AgentCreateRequest(
                name=request.name,
                description=request.description,
                prompt=request.prompt,
                allowed_tools=request.allowed_tools,
                allowed_skills=request.allowed_skills,
                max_turns=request.max_turns,
                input_schema=request.input_schema,
                output_mode=request.output_mode,
                output_schema=request.output_schema,
            )
        )
        return _created_agent_result(detail)

    if isinstance(request, AgentBuilderUseTemplateRequest):
        definition_service = AgentDefinitionService(
            session,
            context.organisation_id,
            user_id,
        )
        copies = await definition_service.access.list_template_copies({request.template_id})
        if copies and not request.confirm_duplicate:
            return {
                "status": "needs_confirmation",
                "message": "You already have an agent copied from this template.",
                "existing_agents": [_template_copy_result(copy) for copy in copies],
            }
        detail = await definition_service.use_template(request.template_id, version=request.version)
        return _created_agent_result(detail)

    trigger_service = AutomationTriggerService(
        session,
        context.organisation_id,
        user_id,
        is_admin=False,
    )
    catalog = await trigger_service.get_catalog()
    if _trigger_family(catalog.model_dump(mode="json"), request.trigger_key) == "webhook":
        return {
            "status": "unsupported",
            "message": (
                "Create webhook triggers in the Agents UI so one-time credentials stay "
                "outside chat and tool-result history."
            ),
        }

    trigger_detail = await trigger_service.create_trigger(
        request.agent_id,
        AutomationTriggerCreateRequest(
            name=request.name,
            trigger_key=request.trigger_key,
            status=request.status,
            config=request.config,
            watch=request.watch,
            filter=request.filter,
            principal_mode="personal",
            scope={"type": "owned_or_created_by_principal", "config": {}},
            audience={"mode": "principal", "user_ids": []},
            task={"goal": request.goal},
        ),
    )
    data = trigger_detail.model_dump(mode="json")
    return {
        "status": "created",
        "trigger_id": data["trigger_id"],
        "name": data["name"],
        "trigger_key": data["trigger_key"],
        "trigger_status": data["status"],
        "display_status": data["display_status"],
        "agent_id": str(request.agent_id),
    }


async def _change_agent_builder(
    context: AgentContext,
    request: AgentBuilderChangeRequest,
) -> dict[str, Any]:
    """Execute one durable change with exactly one commit or rollback."""

    if context.user_id is None:
        return {
            "status": "error",
            "message": "Agent management requires an authenticated user.",
        }

    # Each tool call is one durable product action. Agent creation and later
    # trigger attachment intentionally remain separate valid transactions.
    async with context.acquire_session() as session:
        try:
            result = await _dispatch_agent_builder_change(session, context, request)
            if result["status"] == "created":
                await session.commit()
            else:
                await session.rollback()
            return result
        except (AgentServiceError, AutomationServiceError) as exc:
            await session.rollback()
            return _service_error_result(exc)
        except Exception:
            await session.rollback()
            logger.exception("Agent builder mutation failed action=%s", request.action)
            return {
                "status": "error",
                "message": "Agent management failed unexpectedly.",
            }


def create_inspect_agent_builder(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable:
    """Create the read-only tool used to inspect live agent-builder metadata."""

    async def inspect_agent_builder(request: AgentBuilderInspectRequest) -> dict[str, Any]:
        """Inspect agents, templates, capabilities, or personal trigger metadata.

        Use overview before creating an agent, trigger_catalog before choosing a
        trigger, and trigger_options to resolve user-facing labels. This tool is
        read-only and always uses the authenticated user's ownership boundary.
        """

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="InspectAgentBuilder",
            tool_func=_inspect_agent_builder,
            parameters={"context": context, "request": request},
            description="Inspecting your agents, templates, and automation options",
            fallback_value={
                "status": "error",
                "message": "Could not inspect agent management options.",
            },
        )
        return dict(result)

    return inspect_agent_builder


def create_change_agent_builder(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable:
    """Create the mutation tool for private agents and personal trigger bindings."""

    async def change_agent_builder(request: AgentBuilderChangeRequest) -> dict[str, Any]:
        """Create a private agent, use a template, or attach a personal trigger.

        Use only after the user has clearly selected the definition or template.
        Duplicate template copies require explicit confirmation, and trigger
        identity/scope/audience are always derived from authenticated context.
        """

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ChangeAgentBuilder",
            tool_func=_change_agent_builder,
            parameters={"context": context, "request": request},
            description="Changing your private agents or personal automation",
            fallback_value={
                "status": "error",
                "message": "Could not change agent management settings.",
            },
        )
        return dict(result)

    return change_agent_builder


__all__ = [
    "AgentBuilderAttachTriggerRequest",
    "AgentBuilderChangeRequest",
    "AgentBuilderCreateRequest",
    "AgentBuilderInspectRequest",
    "AgentBuilderOverviewRequest",
    "AgentBuilderTriggerCatalogRequest",
    "AgentBuilderTriggerOptionsRequest",
    "AgentBuilderUseTemplateRequest",
    "create_change_agent_builder",
    "create_inspect_agent_builder",
]
