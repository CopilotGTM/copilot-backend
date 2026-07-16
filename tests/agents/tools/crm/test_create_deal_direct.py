from __future__ import annotations

from datetime import datetime
from inspect import signature
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from src.agents.context import AgentContext
from src.agents.tools.crm.create_deal import create_create_deal, create_deal_direct
from src.service.crm.internal.deal_write_service import DealWriteServiceError
from src.service.orgnisation.organisation_policy_service import OrganisationPolicyDeniedError


class _AsyncSessionContext:
    def __init__(self, session: MagicMock) -> None:
        self.session = session

    async def __aenter__(self) -> MagicMock:
        return self.session

    async def __aexit__(self, *_exc: object) -> bool:
        return False


async def _single_session(session: MagicMock):
    yield session


def _context(*, user_id: UUID | None = None) -> AgentContext:
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=user_id or UUID("22222222-2222-2222-2222-222222222222"),
        channel="teams",
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(MagicMock()))
    return context


@pytest.fixture(autouse=True)
def _allow_crm_write_scope_by_default():
    with patch(
        "src.agents.tools.crm.create_deal.ensure_crm_write_scope",
        new=AsyncMock(return_value=None),
    ):
        yield


@pytest.mark.asyncio
async def test_create_deal_direct_requires_name() -> None:
    result = await create_deal_direct(context=_context(), name=" ")

    assert result["status"] == "error"
    assert "deal name" in result["message"].lower()


@pytest.mark.asyncio
async def test_create_deal_direct_rejects_when_org_policy_disables_scope() -> None:
    context = _context()

    with (
        patch(
            "src.agents.tools.crm.create_deal.ensure_crm_write_scope",
            new=AsyncMock(
                side_effect=OrganisationPolicyDeniedError(
                    "Internal CRM write scope 'deals:create' is disabled for this organisation",
                    code="crm_write_scope_disabled",
                )
            ),
        ),
        patch("src.agents.tools.crm.create_deal.DealWriteService") as service_cls,
    ):
        result = await create_deal_direct(context=context, name="Acme Expansion")

    service_cls.assert_not_called()
    assert result == {
        "status": "error",
        "message": "Internal CRM write scope 'deals:create' is disabled for this organisation",
    }


@pytest.mark.asyncio
async def test_create_deal_direct_creates_name_only_with_defaults_and_context_owner() -> None:
    context = _context()
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    master_session = MagicMock()
    deal_id = uuid4()
    pipeline_id = uuid4()
    stage_id = uuid4()
    owner_id = context.user_id
    company_id = uuid4()
    deal = SimpleNamespace(
        deal_id=deal_id,
        name="Acme Expansion",
        amount=None,
        currency=None,
        pipeline_id=pipeline_id,
        pipeline_name="Sales Pipeline",
        stage_id=stage_id,
        stage_name="Discovery",
        owner_id=owner_id,
        owner_name="Nikhil",
        deal_status="OPEN",
        company_id=company_id,
    )

    with (
        patch(
            "src.agents.tools.crm.create_deal.acquire_master_async_session",
            return_value=_single_session(master_session),
        ),
        patch("src.agents.tools.crm.create_deal.DealWriteService") as service_cls,
    ):
        service = MagicMock()
        service.create_deal = AsyncMock(return_value=deal)
        service_cls.return_value = service

        result = await create_deal_direct(context=context, name=" Acme Expansion ")

    service_cls.assert_called_once_with(
        org_session=org_session,
        master_session=master_session,
        organisation_id=context.organisation_id,
        actor_user_id=context.user_id,
    )
    service.create_deal.assert_awaited_once_with(
        {
            "name": "Acme Expansion",
            "owner_user_id": context.user_id,
            "created_via": "agent_create_deal",
        }
    )
    org_session.commit.assert_awaited_once()
    org_session.rollback.assert_not_awaited()
    assert result == {
        "status": "created",
        "deal_id": str(deal_id),
        "name": "Acme Expansion",
        "pipeline_id": str(pipeline_id),
        "pipeline_name": "Sales Pipeline",
        "stage_id": str(stage_id),
        "stage_name": "Discovery",
        "owner_user_id": str(owner_id),
        "owner_name": "Nikhil",
        "deal_status": "OPEN",
        "company_id": str(company_id),
        "message": (
            f"Created deal **Acme Expansion** (Deal ID: {deal_id}) in "
            "Sales Pipeline > Discovery. Owner: Nikhil. Status: OPEN."
        ),
        "_bot_channel_payload": {
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
                                    "text": "Deal created",
                                    "size": "Medium",
                                    "weight": "Bolder",
                                    "wrap": True,
                                    "color": "Good",
                                },
                                {
                                    "type": "FactSet",
                                    "facts": [
                                        {"title": "Name", "value": "Acme Expansion"},
                                        {"title": "Deal ID", "value": str(deal_id)},
                                        {"title": "Owner", "value": "Nikhil"},
                                        {"title": "Status", "value": "OPEN"},
                                        {"title": "Pipeline", "value": "Sales Pipeline"},
                                        {"title": "Stage", "value": "Discovery"},
                                    ],
                                },
                                {
                                    "type": "TextBlock",
                                    "text": (
                                        "TODO: Add next-action buttons for add stakeholder, "
                                        "add note, and update stage."
                                    ),
                                    "isVisible": False,
                                },
                            ],
                        },
                    }
                ],
            },
            "text_fallback": (
                f"Created deal Acme Expansion (Deal ID: {deal_id}) in "
                "Sales Pipeline > Discovery. Owner: Nikhil. Status: OPEN."
            ),
            "attachments": None,
            "source_text": None,
        },
    }


@pytest.mark.asyncio
async def test_create_deal_direct_defaults_currency_when_amount_has_no_currency() -> None:
    context = _context()
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Budget Deal",
        amount=12000,
        currency="USD",
        pipeline_name="Sales Pipeline",
        stage_name="Discovery",
    )

    with (
        patch(
            "src.agents.tools.crm.create_deal.acquire_master_async_session",
            return_value=_single_session(MagicMock()),
        ),
        patch("src.agents.tools.crm.create_deal.DealWriteService") as service_cls,
    ):
        service = MagicMock()
        service.create_deal = AsyncMock(return_value=deal)
        service_cls.return_value = service

        result = await create_deal_direct(context=context, name="Budget Deal", amount=12000)

    payload = service.create_deal.await_args.args[0]
    assert payload["amount"] == 12000
    assert payload["currency"] == "USD"
    assert result["amount"] == 12000
    assert result["currency"] == "USD"
    assert "Amount: USD 12,000.00" in result["message"]


@pytest.mark.asyncio
async def test_create_deal_direct_passes_custom_columns_to_writer() -> None:
    context = _context()
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Custom Field Deal",
        amount=None,
        currency=None,
        pipeline_name="Sales Pipeline",
        stage_name="Discovery",
    )

    with (
        patch(
            "src.agents.tools.crm.create_deal.acquire_master_async_session",
            return_value=_single_session(MagicMock()),
        ),
        patch("src.agents.tools.crm.create_deal.DealSchemaService") as schema_cls,
        patch("src.agents.tools.crm.create_deal.DealWriteService") as service_cls,
    ):
        schema = MagicMock()
        schema.get_schema = AsyncMock(return_value=SimpleNamespace(fields=[]))
        schema_cls.return_value = schema
        service = MagicMock()
        service.create_deal = AsyncMock(return_value=deal)
        service_cls.return_value = service

        await create_deal_direct(
            context=context,
            name="Custom Field Deal",
            custom_columns={"procurement_stage": "legal"},
        )

    payload = service.create_deal.await_args.args[0]
    assert payload["custom_properties"] == {"procurement_stage": "legal"}


@pytest.mark.asyncio
async def test_create_deal_direct_rejects_non_object_custom_columns() -> None:
    result = await create_deal_direct(
        context=_context(),
        name="Bad Custom Columns",
        custom_columns="procurement_stage=legal",  # type: ignore[arg-type]
    )

    assert result == {
        "status": "error",
        "message": "custom_columns must be an object keyed by custom field key.",
    }


@pytest.mark.asyncio
async def test_create_deal_direct_uses_schema_for_missing_required_custom_fields() -> None:
    context = _context()
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    required_field = SimpleNamespace(
        key="procurement_stage",
        label="Procurement Stage",
        type="select",
        write_path="custom_columns.procurement_stage",
        internal_write=True,
        internal_write_validation={"required": True, "option_membership": True},
        options={"values": [{"id": "legal", "value": "legal", "label": "Legal"}]},
    )

    with (
        patch(
            "src.agents.tools.crm.create_deal.acquire_master_async_session",
            return_value=_single_session(MagicMock()),
        ),
        patch("src.agents.tools.crm.create_deal.DealSchemaService") as schema_cls,
        patch("src.agents.tools.crm.create_deal.DealWriteService") as service_cls,
    ):
        schema = MagicMock()
        schema.get_schema = AsyncMock(return_value=SimpleNamespace(fields=[required_field]))
        schema_cls.return_value = schema

        result = await create_deal_direct(context=context, name="Needs Custom Fields")

    service_cls.assert_not_called()
    org_session.commit.assert_not_awaited()
    assert result["status"] == "needs_input"
    assert result["schema_endpoint"] == "/users/crm/schema?entity=deal"
    assert result["required_custom_fields"] == [
        {
            "key": "procurement_stage",
            "label": "Procurement Stage",
            "type": "select",
            "write_path": "custom_columns.procurement_stage",
            "options": {"values": [{"id": "legal", "value": "legal", "label": "Legal"}]},
        }
    ]


@pytest.mark.asyncio
async def test_create_deal_direct_required_schema_accepts_legacy_custom_key_prefix() -> None:
    context = _context()
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    required_field = SimpleNamespace(
        key="procurement_stage",
        label="Procurement Stage",
        type="select",
        write_path="custom_columns.procurement_stage",
        internal_write=True,
        internal_write_validation={"required": True},
        options={},
    )
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Legacy Custom Prefix",
        amount=None,
        currency=None,
        pipeline_name="Sales Pipeline",
        stage_name="Discovery",
    )

    with (
        patch(
            "src.agents.tools.crm.create_deal.acquire_master_async_session",
            return_value=_single_session(MagicMock()),
        ),
        patch("src.agents.tools.crm.create_deal.DealSchemaService") as schema_cls,
        patch("src.agents.tools.crm.create_deal.DealWriteService") as service_cls,
    ):
        schema = MagicMock()
        schema.get_schema = AsyncMock(return_value=SimpleNamespace(fields=[required_field]))
        schema_cls.return_value = schema
        service = MagicMock()
        service.create_deal = AsyncMock(return_value=deal)
        service_cls.return_value = service

        result = await create_deal_direct(
            context=context,
            name="Legacy Custom Prefix",
            custom_columns={"custom_procurement_stage": "legal"},
        )

    assert result["status"] == "created"
    payload = service.create_deal.await_args.args[0]
    assert payload["custom_properties"] == {"custom_procurement_stage": "legal"}


def test_create_deal_direct_contract_does_not_accept_actual_close_date() -> None:
    assert "close_date" not in signature(create_deal_direct).parameters


@pytest.mark.asyncio
async def test_create_deal_direct_maps_expected_close_date() -> None:
    context = _context()
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Renewal Deal",
        amount=None,
        currency=None,
        pipeline_name="Sales Pipeline",
        stage_name="Discovery",
    )

    with (
        patch(
            "src.agents.tools.crm.create_deal.acquire_master_async_session",
            return_value=_single_session(MagicMock()),
        ),
        patch("src.agents.tools.crm.create_deal.DealWriteService") as service_cls,
    ):
        service = MagicMock()
        service.create_deal = AsyncMock(return_value=deal)
        service_cls.return_value = service

        await create_deal_direct(
            context=context,
            name="Renewal Deal",
            expected_close_date="2027-03-31",
        )

    payload = service.create_deal.await_args.args[0]
    assert payload["expected_close_date"] == datetime(2027, 3, 31)
    assert "close_date" not in payload


@pytest.mark.asyncio
async def test_create_deal_direct_returns_structured_failure_for_write_service_error() -> None:
    context = _context()
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))

    with (
        patch(
            "src.agents.tools.crm.create_deal.acquire_master_async_session",
            return_value=_single_session(MagicMock()),
        ),
        patch("src.agents.tools.crm.create_deal.DealWriteService") as service_cls,
    ):
        service = MagicMock()
        service.create_deal = AsyncMock(
            side_effect=DealWriteServiceError("No default pipeline found", status_code=500)
        )
        service_cls.return_value = service

        result = await create_deal_direct(context=context, name="Pipeline Missing")

    org_session.commit.assert_not_awaited()
    org_session.rollback.assert_awaited_once()
    assert result == {
        "status": "error",
        "message": "Could not create deal: No default pipeline found",
    }


@pytest.mark.asyncio
async def test_create_deal_tool_uses_direct_create_path() -> None:
    context = _context()
    tool = create_create_deal(context, lambda: None)
    pipeline_id = uuid4()
    stage_id = uuid4()

    with patch("src.agents.tools.crm.create_deal.create_deal_direct") as direct_create:
        direct_create.return_value = {
            "status": "created",
            "name": "Acme",
            "_bot_channel_payload": {"type": "message", "content": {"text": "card"}},
        }

        result = await tool(
            name="Acme",
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            custom_columns={"procurement_stage": "legal"},
        )

    assert result == {"status": "created", "name": "Acme"}
    direct_create.assert_awaited_once()
    assert direct_create.await_args.kwargs["pipeline_id"] == pipeline_id
    assert direct_create.await_args.kwargs["stage_id"] == stage_id
    assert direct_create.await_args.kwargs["custom_columns"] == {"procurement_stage": "legal"}


@pytest.mark.asyncio
async def test_create_deal_direct_passes_pipeline_and_stage_ids_to_writer() -> None:
    context = _context()
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    pipeline_id = uuid4()
    stage_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Pipeline-specific Deal",
        amount=None,
        currency=None,
        pipeline_id=pipeline_id,
        pipeline_name="Enterprise Sales",
        stage_id=stage_id,
        stage_name="Proposal",
    )

    with (
        patch(
            "src.agents.tools.crm.create_deal.acquire_master_async_session",
            return_value=_single_session(MagicMock()),
        ),
        patch("src.agents.tools.crm.create_deal.DealWriteService") as service_cls,
    ):
        service = MagicMock()
        service.create_deal = AsyncMock(return_value=deal)
        service_cls.return_value = service

        result = await create_deal_direct(
            context=context,
            name="Pipeline-specific Deal",
            pipeline_id=pipeline_id,
            stage_id=stage_id,
        )

    payload = service.create_deal.await_args.args[0]
    assert payload["pipeline_id"] == pipeline_id
    assert payload["stage_id"] == stage_id
    assert result["pipeline_id"] == str(pipeline_id)
    assert result["stage_id"] == str(stage_id)


@pytest.mark.asyncio
async def test_create_deal_direct_builds_slack_success_card() -> None:
    context = _context()
    context.channel = "slack"
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Slack Deal",
        amount=50000,
        currency="USD",
        pipeline_name="Sales Pipeline",
        stage_name="Proposal",
        company_name="Acme",
    )

    with (
        patch(
            "src.agents.tools.crm.create_deal.acquire_master_async_session",
            return_value=_single_session(MagicMock()),
        ),
        patch("src.agents.tools.crm.create_deal.DealWriteService") as service_cls,
    ):
        service = MagicMock()
        service.create_deal = AsyncMock(return_value=deal)
        service_cls.return_value = service

        result = await create_deal_direct(
            context=context,
            name="Slack Deal",
            amount=50000,
            account_name="Acme",
        )

    channel_payload = result["_bot_channel_payload"]
    assert channel_payload["type"] == "message"
    assert channel_payload["text_fallback"] == (
        f"Created deal Slack Deal (Deal ID: {deal.deal_id}) in Sales Pipeline > Proposal."
    )
    blocks = channel_payload["content"]["blocks"]
    assert blocks[0]["type"] == "header"
    assert blocks[0]["text"]["text"] == "Deal created"
    assert any(
        field["text"] == "*Amount*\nUSD 50,000.00"
        for block in blocks
        if block.get("type") == "section" and "fields" in block
        for field in block["fields"]
    )
    assert any(
        field["text"] == f"*Deal ID*\n{deal.deal_id}"
        for block in blocks
        if block.get("type") == "section" and "fields" in block
        for field in block["fields"]
    )


@pytest.mark.asyncio
async def test_create_deal_direct_builds_whatsapp_text_payload() -> None:
    context = _context()
    context.channel = "whatsapp"
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="WhatsApp Deal",
        amount=7500,
        currency="USD",
        pipeline_name="Sales Pipeline",
        stage_name="Discovery",
        company_name="Acme",
    )

    with (
        patch(
            "src.agents.tools.crm.create_deal.acquire_master_async_session",
            return_value=_single_session(MagicMock()),
        ),
        patch("src.agents.tools.crm.create_deal.DealWriteService") as service_cls,
    ):
        service = MagicMock()
        service.create_deal = AsyncMock(return_value=deal)
        service_cls.return_value = service

        result = await create_deal_direct(
            context=context,
            name="WhatsApp Deal",
            amount=7500,
            account_name="Acme",
        )

    channel_payload = result["_bot_channel_payload"]
    assert channel_payload == {
        "type": "message",
        "content": {
            "text": (
                "Deal created\n"
                "Name: WhatsApp Deal\n"
                f"Deal ID: {deal.deal_id}\n"
                "Amount: USD 7,500.00\n"
                "Pipeline: Sales Pipeline\n"
                "Stage: Discovery\n"
                "Account: Acme"
            )
        },
        "text_fallback": (
            f"Created deal WhatsApp Deal (Deal ID: {deal.deal_id}) in Sales Pipeline > Discovery."
        ),
        "attachments": None,
        "source_text": None,
    }


@pytest.mark.asyncio
async def test_create_deal_direct_web_includes_ui_card_payload() -> None:
    context = _context()
    context.channel = "web"
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Web Deal",
        amount=None,
        currency=None,
        pipeline_name="Sales Pipeline",
        stage_name="Discovery",
    )

    with (
        patch(
            "src.agents.tools.crm.create_deal.acquire_master_async_session",
            return_value=_single_session(MagicMock()),
        ),
        patch("src.agents.tools.crm.create_deal.DealWriteService") as service_cls,
    ):
        service = MagicMock()
        service.create_deal = AsyncMock(return_value=deal)
        service_cls.return_value = service

        result = await create_deal_direct(context=context, name="Web Deal")

    assert result["status"] == "created"
    assert result["name"] == "Web Deal"
    assert result["ui_card"] == {
        "kind": "deal_created_confirmation",
        "version": "1.0",
        "tone": "success",
        "title": "Deal created",
        "message": (
            f"Created deal Web Deal (Deal ID: {deal.deal_id}) in Sales Pipeline > Discovery."
        ),
        "entity": {
            "type": "deal",
            "id": str(deal.deal_id),
            "name": "Web Deal",
        },
        "facts": [
            {"label": "Name", "value": "Web Deal"},
            {"label": "Deal ID", "value": str(deal.deal_id)},
            {"label": "Pipeline", "value": "Sales Pipeline"},
            {"label": "Stage", "value": "Discovery"},
        ],
        "actions": [],
    }
    assert result["_bot_channel_payload"] == {
        "type": "ui_card",
        "content": {"card": result["ui_card"]},
        "text_fallback": (
            f"Created deal Web Deal (Deal ID: {deal.deal_id}) in Sales Pipeline > Discovery."
        ),
        "attachments": None,
        "source_text": None,
    }


@pytest.mark.asyncio
async def test_create_deal_direct_voice_includes_ui_card_payload() -> None:
    context = _context()
    context.channel = "voice"
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Voice Deal",
        amount=None,
        currency=None,
        pipeline_name="Sales Pipeline",
        stage_name="Discovery",
    )

    with (
        patch(
            "src.agents.tools.crm.create_deal.acquire_master_async_session",
            return_value=_single_session(MagicMock()),
        ),
        patch("src.agents.tools.crm.create_deal.DealWriteService") as service_cls,
    ):
        service = MagicMock()
        service.create_deal = AsyncMock(return_value=deal)
        service_cls.return_value = service

        result = await create_deal_direct(context=context, name="Voice Deal")

    assert result["ui_card"]["kind"] == "deal_created_confirmation"
    assert result["ui_card"]["entity"] == {
        "type": "deal",
        "id": str(deal.deal_id),
        "name": "Voice Deal",
    }
    assert result["_bot_channel_payload"] == {
        "type": "ui_card",
        "content": {"card": result["ui_card"]},
        "text_fallback": (
            f"Created deal Voice Deal (Deal ID: {deal.deal_id}) in Sales Pipeline > Discovery."
        ),
        "attachments": None,
        "source_text": None,
    }
