"""Contracts for the GeneralAgent-only agent builder tools."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from agno.tools.function import Function
from src.agents.context import AgentContext
from src.agents.tools import agent_builder
from src.agents.tools.agent_builder import (
    AgentBuilderAttachTriggerRequest,
    AgentBuilderCreateRequest,
    AgentBuilderOverviewRequest,
    AgentBuilderTriggerCatalogRequest,
    AgentBuilderTriggerOptionsRequest,
    AgentBuilderUseTemplateRequest,
    create_change_agent_builder,
    create_inspect_agent_builder,
)
from src.contracts.agent_models import (
    AgentCapabilityCatalog,
    AgentCapabilityItem,
    AgentListResponse,
)
from src.contracts.automation_trigger_models import AutomationTriggerOptionSearchResponse
from src.service.agents.errors import AgentServiceError
from src.service.agents.template_metadata import list_agent_templates


class _Session:
    """Small session double shared by read and later transaction tests."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def _context_with_session(session: _Session, *, user: bool = True) -> AgentContext:
    context = AgentContext(
        organisation_id=uuid4(),
        user_id=uuid4() if user else None,
        channel="web",
    )

    @asynccontextmanager
    async def acquire_session():
        yield session

    context.acquire_session = acquire_session  # type: ignore[method-assign]
    return context


class _Dumpable:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        assert mode == "json"
        return self.value


def test_agent_builder_exposes_two_strict_discriminated_tools() -> None:
    """Keep the model-facing surface small while rejecting mixed action fields."""
    context = AgentContext(organisation_id=uuid4(), user_id=uuid4(), channel="web")

    inspect_tool = create_inspect_agent_builder(context, lambda: None)
    change_tool = create_change_agent_builder(context, lambda: None)
    inspect_schema = Function.from_callable(inspect_tool, strict=True).parameters
    change_schema = Function.from_callable(change_tool, strict=True).parameters

    assert inspect_tool.__name__ == "inspect_agent_builder"
    assert change_tool.__name__ == "change_agent_builder"
    assert inspect_schema["additionalProperties"] is False
    assert change_schema["additionalProperties"] is False
    assert len(inspect_schema["properties"]["request"]["anyOf"]) == 3
    assert len(change_schema["properties"]["request"]["anyOf"]) == 3
    assert all(
        branch["additionalProperties"] is False
        for branch in inspect_schema["properties"]["request"]["anyOf"]
    )
    assert all(
        branch["additionalProperties"] is False
        for branch in change_schema["properties"]["request"]["anyOf"]
    )


def test_agent_builder_write_schema_has_no_identity_or_authority_inputs() -> None:
    """Tenant and execution authority must come only from trusted AgentContext."""
    context = AgentContext(organisation_id=uuid4(), user_id=uuid4(), channel="web")
    tool = create_change_agent_builder(context, lambda: None)
    schema = json.dumps(Function.from_callable(tool, strict=True).parameters)

    for forbidden in (
        "organisation_id",
        "user_id",
        "principal_mode",
        "execution_principal_id",
        '"scope"',
        '"audience"',
        "source_type",
        "visibility",
    ):
        assert forbidden not in schema


@pytest.mark.asyncio
async def test_inspection_requires_an_authenticated_user_before_opening_a_session() -> None:
    """Organisation membership alone cannot inspect a user's private control plane."""
    context = _context_with_session(_Session(), user=False)

    @asynccontextmanager
    async def fail_if_opened():
        raise AssertionError("session must not open")
        yield

    context.acquire_session = fail_if_opened  # type: ignore[method-assign]
    tool = create_inspect_agent_builder(context, lambda: None)

    result = await tool(AgentBuilderOverviewRequest(action="overview"))

    assert result == {
        "status": "error",
        "message": "Agent management requires an authenticated user.",
    }


@pytest.mark.asyncio
async def test_overview_annotates_templates_with_only_owned_live_copies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return one batched copy lookup and derive automation safety from the registry."""
    session = _Session()
    context = _context_with_session(session)
    template = list_agent_templates()[0]
    seen_template_ids: set[Any] = set()

    class FakeAccess:
        async def list_template_copies(self, template_ids: set[Any]) -> list[Any]:
            seen_template_ids.update(template_ids)
            return [
                SimpleNamespace(
                    agent_id=uuid4(),
                    name="My Deal Review",
                    status="active",
                    revision=2,
                    source_agent_id=template.template_id,
                    source_version=template.version,
                )
            ]

    class FakeDefinitionService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.access = FakeAccess()

        async def list_agents(self, **kwargs: Any) -> AgentListResponse:
            assert kwargs == {"status": None, "limit": 20, "offset": 0}
            return AgentListResponse(items=[], total_count=0, limit=20, offset=0)

    catalog = AgentCapabilityCatalog(
        tools=[
            AgentCapabilityItem(key="search_deals", label="Search Deals", category="crm"),
            AgentCapabilityItem(key="send_email", label="Send Email", category="communication"),
        ],
        skills=[
            AgentCapabilityItem(key="pipeline-review", label="Pipeline Review", category="skill")
        ],
        output_delivery_channels=[],
    )
    monkeypatch.setattr(agent_builder, "AgentDefinitionService", FakeDefinitionService)
    monkeypatch.setattr(agent_builder, "build_agent_capability_catalog", lambda: catalog)
    monkeypatch.setattr(agent_builder, "RETRY_UNSAFE_AUTOMATION_TOOLS", frozenset({"send_email"}))
    tool = create_inspect_agent_builder(context, lambda: None)

    result = await tool(AgentBuilderOverviewRequest(action="overview"))

    assert result["status"] == "ok"
    assert seen_template_ids == {template.template_id}
    assert result["templates"][0]["existing_agents"][0]["name"] == "My Deal Review"
    assert result["capabilities"]["tools"] == [
        {
            "key": "search_deals",
            "label": "Search Deals",
            "category": "crm",
            "automation_compatible": True,
        },
        {
            "key": "send_email",
            "label": "Send Email",
            "category": "communication",
            "automation_compatible": False,
        },
    ]


@pytest.mark.asyncio
async def test_trigger_catalog_keeps_personal_definitions_and_marks_webhooks_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teach trigger families from live metadata without enabling secret-bearing webhooks."""
    session = _Session()
    context = _context_with_session(session)
    catalog = {
        "categories": [
            {
                "category_key": "crm",
                "label": "CRM",
                "resources": [
                    {
                        "resource_key": "deal",
                        "label": "Deals",
                        "triggers": [
                            {
                                "trigger_key": "crm.deal.changed",
                                "family": "event",
                                "principal_modes": ["personal", "organisation"],
                            },
                            {
                                "trigger_key": "crm.deal.org_only",
                                "family": "event",
                                "principal_modes": ["organisation"],
                            },
                            {
                                "trigger_key": "webhook.external",
                                "family": "webhook",
                                "principal_modes": ["personal"],
                            },
                        ],
                    },
                    {
                        "resource_key": "empty",
                        "label": "Empty",
                        "triggers": [],
                    },
                ],
            }
        ],
        "option_sets": {"pipelines": [], "enums": {}},
        "limits": {"max_filter_conditions": 20},
    }

    class FakeTriggerService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def get_catalog(self) -> _Dumpable:
            return _Dumpable(catalog)

    monkeypatch.setattr(agent_builder, "AutomationTriggerService", FakeTriggerService)
    tool = create_inspect_agent_builder(context, lambda: None)

    result = await tool(AgentBuilderTriggerCatalogRequest(action="trigger_catalog"))

    triggers = result["catalog"]["categories"][0]["resources"][0]["triggers"]
    assert [item["trigger_key"] for item in triggers] == [
        "crm.deal.changed",
        "webhook.external",
    ]
    assert triggers[0]["chat_supported"] is True
    assert triggers[1]["chat_supported"] is False
    assert "Agents UI" in triggers[1]["chat_unsupported_reason"]
    assert len(result["catalog"]["categories"][0]["resources"]) == 1


@pytest.mark.asyncio
async def test_trigger_option_search_fixes_personal_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model can choose a field but cannot broaden its execution principal."""
    session = _Session()
    context = _context_with_session(session)
    seen_request: Any = None

    class FakeTriggerService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def search_trigger_options(self, request: Any) -> Any:
            nonlocal seen_request
            seen_request = request
            return AutomationTriggerOptionSearchResponse(items=[], next_cursor=None)

    monkeypatch.setattr(agent_builder, "AutomationTriggerService", FakeTriggerService)
    tool = create_inspect_agent_builder(context, lambda: None)

    result = await tool(
        AgentBuilderTriggerOptionsRequest(
            action="trigger_options",
            trigger_key="crm.deal.stage_changed",
            control="filter",
            field_path="deal.stage_id",
            query="Discovery",
        )
    )

    assert result == {"status": "ok", "options": {"items": [], "next_cursor": None}}
    assert seen_request.principal_mode == "personal"
    assert seen_request.field_path == "deal.stage_id"


@pytest.mark.asyncio
async def test_create_agent_maps_only_editable_fields_and_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provenance and ownership remain service-owned while user fields pass through."""
    session = _Session()
    context = _context_with_session(session)
    seen_body: Any = None
    agent_id = uuid4()

    class FakeDefinitionService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def create_agent(self, body: Any) -> _Dumpable:
            nonlocal seen_body
            seen_body = body
            return _Dumpable(
                {
                    "agent_id": str(agent_id),
                    "name": body.name,
                    "source_type": "user",
                    "revision": 1,
                    "allowed_tools": body.allowed_tools,
                    "allowed_skills": body.allowed_skills,
                    "automation_eligibility": {
                        "personal": {"available": True},
                        "organisation": {"available": False},
                    },
                }
            )

    monkeypatch.setattr(agent_builder, "AgentDefinitionService", FakeDefinitionService)
    tool = create_change_agent_builder(context, lambda: None)

    result = await tool(
        AgentBuilderCreateRequest(
            action="create_agent",
            name="Pipeline Coach",
            prompt="Review my pipeline.",
            allowed_tools=["search_deals"],
            allowed_skills=["pipeline-review"],
        )
    )

    assert result == {
        "status": "created",
        "agent_id": str(agent_id),
        "name": "Pipeline Coach",
        "source_type": "user",
        "revision": 1,
        "allowed_tools": ["search_deals"],
        "allowed_skills": ["pipeline-review"],
        "automation_eligibility": {
            "personal": {"available": True},
            "organisation": {"available": False},
        },
    }
    assert seen_body.model_dump(exclude_defaults=False) == {
        "allowed_tools": ["search_deals"],
        "allowed_skills": ["pipeline-review"],
        "name": "Pipeline Coach",
        "description": None,
        "prompt": "Review my pipeline.",
        "model_name": None,
        "agent_model_config": {},
        "max_turns": None,
        "input_schema": None,
        "output_mode": "text",
        "output_schema": None,
    }
    assert session.commits == 1
    assert session.rollbacks == 0


@pytest.mark.asyncio
async def test_create_agent_rolls_back_expected_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Known service messages are safe to return, but failed writes never commit."""
    session = _Session()
    context = _context_with_session(session)

    class FakeDefinitionService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def create_agent(self, body: Any) -> Any:
            raise AgentServiceError("Unknown agent tools: made_up")

    monkeypatch.setattr(agent_builder, "AgentDefinitionService", FakeDefinitionService)
    tool = create_change_agent_builder(context, lambda: None)

    result = await tool(
        AgentBuilderCreateRequest(
            action="create_agent",
            name="Broken",
            prompt="Use an invalid capability.",
            allowed_tools=["made_up"],
        )
    )

    assert result == {"status": "needs_input", "message": "Unknown agent tools: made_up"}
    assert session.commits == 0
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_template_copy_requires_confirmation_when_current_user_has_a_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple copies remain possible, but chat cannot create one silently."""
    session = _Session()
    context = _context_with_session(session)
    template = list_agent_templates()[0]
    existing_id = uuid4()

    class FakeAccess:
        async def list_template_copies(self, template_ids: set[Any]) -> list[Any]:
            assert template_ids == {template.template_id}
            return [
                SimpleNamespace(
                    agent_id=existing_id,
                    name="Existing Review",
                    status="active",
                    revision=1,
                    source_agent_id=template.template_id,
                    source_version=1,
                )
            ]

    class FakeDefinitionService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.access = FakeAccess()

        async def use_template(self, template_id: Any, *, version: int) -> Any:
            raise AssertionError("template must not be copied without confirmation")

    monkeypatch.setattr(agent_builder, "AgentDefinitionService", FakeDefinitionService)
    tool = create_change_agent_builder(context, lambda: None)

    result = await tool(
        AgentBuilderUseTemplateRequest(
            action="use_template",
            template_id=template.template_id,
            version=template.version,
        )
    )

    assert result["status"] == "needs_confirmation"
    assert result["existing_agents"] == [
        {
            "agent_id": str(existing_id),
            "name": "Existing Review",
            "status": "active",
            "revision": 1,
            "source_version": 1,
        }
    ]
    assert session.commits == 0
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_confirmed_template_copy_uses_the_exact_version_and_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The copied definition must never drift to a newer code-owned template version."""
    session = _Session()
    context = _context_with_session(session)
    template = list_agent_templates()[0]
    seen: tuple[Any, int] | None = None
    agent_id = uuid4()

    class FakeAccess:
        async def list_template_copies(self, template_ids: set[Any]) -> list[Any]:
            return [SimpleNamespace(source_agent_id=template.template_id)]

    class FakeDefinitionService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.access = FakeAccess()

        async def use_template(self, template_id: Any, *, version: int) -> _Dumpable:
            nonlocal seen
            seen = (template_id, version)
            return _Dumpable(
                {
                    "agent_id": str(agent_id),
                    "name": template.name,
                    "source_type": "system",
                    "revision": 1,
                    "allowed_tools": template.allowed_tools,
                    "allowed_skills": template.allowed_skills,
                    "automation_eligibility": {"personal": {"available": True}},
                }
            )

    monkeypatch.setattr(agent_builder, "AgentDefinitionService", FakeDefinitionService)
    tool = create_change_agent_builder(context, lambda: None)

    result = await tool(
        AgentBuilderUseTemplateRequest(
            action="use_template",
            template_id=template.template_id,
            version=template.version,
            confirm_duplicate=True,
        )
    )

    assert seen == (template.template_id, template.version)
    assert result["status"] == "created"
    assert result["agent_id"] == str(agent_id)
    assert session.commits == 1


@pytest.mark.asyncio
async def test_attach_trigger_derives_personal_authority_and_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope, audience, and principal identity are immutable server decisions."""
    session = _Session()
    context = _context_with_session(session)
    agent_id = uuid4()
    trigger_id = uuid4()
    seen: tuple[Any, Any] | None = None

    class FakeTriggerService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def get_catalog(self) -> _Dumpable:
            return _Dumpable(
                {
                    "categories": [
                        {
                            "resources": [
                                {
                                    "triggers": [
                                        {
                                            "trigger_key": "crm.deal.stage_changed",
                                            "family": "event",
                                            "principal_modes": ["personal"],
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            )

        async def create_trigger(self, target_id: Any, body: Any) -> _Dumpable:
            nonlocal seen
            seen = (target_id, body)
            return _Dumpable(
                {
                    "trigger_id": str(trigger_id),
                    "name": body.name,
                    "trigger_key": body.trigger_key,
                    "status": body.status.value,
                    "display_status": body.status.value,
                    "target_id": str(target_id),
                }
            )

    monkeypatch.setattr(agent_builder, "AutomationTriggerService", FakeTriggerService)
    tool = create_change_agent_builder(context, lambda: None)

    result = await tool(
        AgentBuilderAttachTriggerRequest(
            action="attach_trigger",
            agent_id=agent_id,
            name="Deal stage review",
            trigger_key="crm.deal.stage_changed",
            status="active",
            goal="Review the new stage.",
        )
    )

    assert seen is not None
    target_id, body = seen
    assert target_id == agent_id
    assert body.principal_mode == "personal"
    assert body.scope.model_dump(mode="json") == {
        "type": "owned_or_created_by_principal",
        "config": {},
    }
    assert body.audience.model_dump(mode="json") == {"mode": "principal", "user_ids": []}
    assert body.task.goal == "Review the new stage."
    assert result == {
        "status": "created",
        "trigger_id": str(trigger_id),
        "name": "Deal stage review",
        "trigger_key": "crm.deal.stage_changed",
        "trigger_status": "active",
        "display_status": "active",
        "agent_id": str(agent_id),
    }
    assert session.commits == 1


@pytest.mark.asyncio
async def test_attach_webhook_stops_before_credentials_are_provisioned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One-time webhook secrets must never enter transparent chat tool history."""
    session = _Session()
    context = _context_with_session(session)

    class FakeTriggerService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def get_catalog(self) -> _Dumpable:
            return _Dumpable(
                {
                    "categories": [
                        {
                            "resources": [
                                {
                                    "triggers": [
                                        {
                                            "trigger_key": "webhook.external",
                                            "family": "webhook",
                                            "principal_modes": ["personal"],
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            )

        async def create_trigger(self, target_id: Any, body: Any) -> Any:
            raise AssertionError("webhook secret provisioning must not start")

    monkeypatch.setattr(agent_builder, "AutomationTriggerService", FakeTriggerService)
    tool = create_change_agent_builder(context, lambda: None)

    result = await tool(
        AgentBuilderAttachTriggerRequest(
            action="attach_trigger",
            agent_id=uuid4(),
            name="Incoming webhook",
            trigger_key="webhook.external",
            status="paused",
        )
    )

    assert result["status"] == "unsupported"
    assert "Agents UI" in result["message"]
    assert session.commits == 0
    assert session.rollbacks == 1
