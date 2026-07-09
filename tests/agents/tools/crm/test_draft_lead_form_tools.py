from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from src.agents.context import AgentContext
from src.agents.tools.crm.draft_lead_form import (
    create_draft_lead_form_direct,
    update_draft_lead_form_direct,
)
from src.agents.tools.registry import ToolName, ToolRegistry
from src.domain.models.organisation_tables.sync.crm.lead_forms import (
    LeadFormFieldDataType,
    LeadFormFieldTargetType,
    LeadFormStatus,
)


class _AsyncSessionContext:
    def __init__(self, session: MagicMock) -> None:
        self.session = session

    async def __aenter__(self) -> MagicMock:
        return self.session

    async def __aexit__(self, *_exc: object) -> bool:
        return False


async def _single_master_session(session: MagicMock):
    yield session


def _context() -> AgentContext:
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        user_email="owner@example.com",
        channel="web",
    )
    org_session = MagicMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    return context


def _form(**overrides: object) -> SimpleNamespace:
    data = {
        "form_id": uuid4(),
        "name": "Website demo",
        "description": "Book a demo",
        "status": LeadFormStatus.DRAFT,
        "current_version": 1,
        "settings": {},
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _field(**overrides: object) -> SimpleNamespace:
    data = {
        "external_key": "email",
        "label": "Email",
        "target_type": LeadFormFieldTargetType.LEAD_FIELD,
        "target_key": "email",
        "data_type": LeadFormFieldDataType.EMAIL,
        "required": True,
        "display_order": 0,
        "default_value": None,
        "validation": {},
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.mark.asyncio
async def test_create_draft_lead_form_forces_draft_and_maps_safe_fields() -> None:
    context = _context()
    master_session = MagicMock()
    master_session.rollback = AsyncMock()
    form = _form()
    saved_fields = [
        _field(),
        _field(
            external_key="company_size",
            label="Company size",
            target_type=LeadFormFieldTargetType.IGNORE,
            target_key=None,
            data_type=LeadFormFieldDataType.NUMBER,
            required=False,
            display_order=1,
        ),
    ]

    with (
        patch(
            "src.agents.tools.crm.draft_lead_form.acquire_master_async_session",
            return_value=_single_master_session(master_session),
        ),
        patch("src.agents.tools.crm.draft_lead_form.LeadFormsService") as service_cls,
    ):
        service = MagicMock()
        service.create_form = AsyncMock(return_value=(form, saved_fields, MagicMock()))
        service_cls.return_value = service

        result = await create_draft_lead_form_direct(
            context=context,
            name=" Website demo ",
            description=" Book a demo ",
            fields=[
                {"label": "Email", "external_key": "email", "required": True},
                {
                    "label": "Company size",
                    "external_key": "company_size",
                    "data_type": "number",
                    "required": True,
                },
            ],
        )

    request = service.create_form.call_args.args[0]
    assert request.status == LeadFormStatus.DRAFT
    assert request.allowed_origins == []
    assert request.settings["visibility"] == "private"
    assert request.fields[0].target_type == LeadFormFieldTargetType.LEAD_FIELD
    assert request.fields[0].target_key == "email"
    assert request.fields[0].data_type == LeadFormFieldDataType.EMAIL
    assert request.fields[1].target_type == LeadFormFieldTargetType.IGNORE
    assert request.fields[1].required is False
    assert result["status"] == "created"
    assert result["form_status"] == "draft"
    assert any("Company size" in warning for warning in result["warnings"])


@pytest.mark.asyncio
async def test_create_draft_lead_form_rejects_empty_fields() -> None:
    result = await create_draft_lead_form_direct(
        context=_context(),
        name="Website demo",
        fields=[],
    )

    assert result == {
        "status": "error",
        "message": "At least one usable form field is required.",
    }


@pytest.mark.asyncio
async def test_update_draft_lead_form_refuses_active_form() -> None:
    context = _context()
    master_session = MagicMock()
    master_session.rollback = AsyncMock()
    active_form = _form(status=LeadFormStatus.ACTIVE)

    with (
        patch(
            "src.agents.tools.crm.draft_lead_form.acquire_master_async_session",
            return_value=_single_master_session(master_session),
        ),
        patch("src.agents.tools.crm.draft_lead_form.LeadFormsService") as service_cls,
    ):
        service = MagicMock()
        service.get_form = AsyncMock(return_value=(active_form, [_field()], MagicMock()))
        service.update_form = AsyncMock()
        service_cls.return_value = service

        result = await update_draft_lead_form_direct(
            context=context,
            form_id=active_form.form_id,
            name="Updated",
        )

    service.update_form.assert_not_awaited()
    assert result == {
        "status": "error",
        "message": "Only draft lead forms can be edited by this tool.",
    }


@pytest.mark.asyncio
async def test_update_draft_lead_form_preserves_fields_and_forces_draft() -> None:
    context = _context()
    master_session = MagicMock()
    master_session.rollback = AsyncMock()
    form = _form()
    existing_field = _field()
    updated_form = _form(name="Updated demo", current_version=2)

    with (
        patch(
            "src.agents.tools.crm.draft_lead_form.acquire_master_async_session",
            return_value=_single_master_session(master_session),
        ),
        patch("src.agents.tools.crm.draft_lead_form.LeadFormsService") as service_cls,
    ):
        service = MagicMock()
        service.get_form = AsyncMock(return_value=(form, [existing_field], MagicMock()))
        service.update_form = AsyncMock(return_value=(updated_form, [existing_field], MagicMock()))
        service_cls.return_value = service

        result = await update_draft_lead_form_direct(
            context=context,
            form_id=str(form.form_id),
            name="Updated demo",
        )

    called_form_id, request = service.update_form.call_args.args
    assert called_form_id == form.form_id
    assert request.status == LeadFormStatus.DRAFT
    assert request.allowed_origins == []
    assert request.fields[0].external_key == "email"
    assert request.fields[0].target_key == "email"
    assert result["status"] == "updated"
    assert result["name"] == "Updated demo"


def test_general_registry_exposes_draft_form_tools_but_voice_does_not() -> None:
    general_tools = ToolRegistry.get_available_tools_for_agent("GeneralAgent")
    voice_tools = ToolRegistry.get_available_tools_for_agent("VoiceRealtimeAgent")

    assert ToolName.CREATE_DRAFT_LEAD_FORM in general_tools
    assert ToolName.UPDATE_DRAFT_LEAD_FORM in general_tools
    assert ToolName.CREATE_DRAFT_LEAD_FORM not in voice_tools
    assert ToolName.UPDATE_DRAFT_LEAD_FORM not in voice_tools
