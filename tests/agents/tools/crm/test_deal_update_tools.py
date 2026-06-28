"""Regression coverage for account, field, and lifecycle deal-update tools."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.agents.context import AgentContext
from src.agents.tools.crm.update_deal_account import (
    ALLOWED_ACCOUNT_FIELDS,
    create_update_deal_account,
)
from src.agents.tools.crm.update_deal_fields import (
    ALLOWED_DEAL_FIELDS,
    _normalize_deal_fields,
    create_update_deal_fields,
)
from src.agents.tools.crm.update_deal_lifecycle import create_update_deal_lifecycle
from src.api.schemas.crm_deal_write_models import DealAccountUpdateRequest, DealUpdateRequest
from src.service.crm.internal.deal_update_policy import ACCOUNT_FIELDS, SAFE_DIRECT_FIELDS


@pytest.mark.asyncio
async def test_update_deal_fields_requires_deal_context():
    org_id = uuid4()

    """Require org/deal context before any deal-field mutation tool call."""
    context = AgentContext(organisation_id=org_id, deal_id=None)
    tool = create_update_deal_fields(context, lambda: None)

    result = await tool(fields={"amount": 12000}, directly_requested=True, source="chat")

    assert result["status"] == "failed"
    assert "Deal context is required" in result["reason"]


@pytest.mark.asyncio
async def test_update_deal_fields_rejects_unsupported_field():
    org_id = uuid4()

    """Reject unsupported fields so only safe updateable attributes can pass."""
    context = AgentContext(organisation_id=org_id, deal_id=uuid4())
    tool = create_update_deal_fields(context, lambda: None)

    result = await tool(fields={"owner_id": str(uuid4())}, directly_requested=True, source="chat")

    assert result["status"] == "skipped"
    assert result["action"] == "update_deal_fields"
    assert "not supported" in result["reason"]


@pytest.mark.asyncio
async def test_lifecycle_tool_requires_action():
    org_id = uuid4()

    """Validate lifecycle action presence before branching to mutation logic."""
    context = AgentContext(organisation_id=org_id, deal_id=uuid4())
    tool = create_update_deal_lifecycle(context, lambda: None)

    result = await tool(action="", directly_requested=True, source="chat")

    assert result["status"] == "failed"
    assert "action is required" in result["reason"]


@pytest.mark.asyncio
async def test_lifecycle_close_lost_without_reason_is_skipped():
    org_id = uuid4()

    """Require close reason for close-lost as a contract-level validation rule."""
    context = AgentContext(organisation_id=org_id, deal_id=uuid4())
    tool = create_update_deal_lifecycle(context, lambda: None)

    result = await tool(
        action="close_lost",
        closed_reason=None,
        directly_requested=True,
        source="chat",
    )

    assert result["status"] == "skipped"
    assert "closed_reason is required" in result["reason"]


@pytest.mark.asyncio
async def test_lifecycle_close_lost_rejects_blank_reason_before_approval():
    org_id = uuid4()

    """Whitespace-only lost reasons are invalid and should not be sent to approval."""
    context = AgentContext(organisation_id=org_id, deal_id=uuid4())
    tool = create_update_deal_lifecycle(context, lambda: None)

    result = await tool(
        action="close_lost",
        closed_reason="   ",
        directly_requested=False,
        source="agent",
    )

    assert result["status"] == "skipped"
    assert "closed_reason is required" in result["reason"]
    assert result["requires_approval"] is False


@pytest.mark.asyncio
async def test_lifecycle_move_stage_requires_stage_id_before_approval():
    org_id = uuid4()

    """Do not send an impossible stage move to approval without a target stage."""
    context = AgentContext(organisation_id=org_id, deal_id=uuid4())
    tool = create_update_deal_lifecycle(context, lambda: None)

    result = await tool(action="move_stage", directly_requested=False, source="agent")

    assert result["status"] == "failed"
    assert "stage_id is required" in result["reason"]
    assert result["requires_approval"] is False


@pytest.mark.asyncio
async def test_lifecycle_move_stage_rejects_invalid_stage_id_before_approval():
    org_id = uuid4()

    """Do not ask approval for a stage move with an invalid stage UUID."""
    context = AgentContext(organisation_id=org_id, deal_id=uuid4())
    tool = create_update_deal_lifecycle(context, lambda: None)

    result = await tool(
        action="move_stage",
        stage_id="not-a-uuid",
        directly_requested=False,
        source="agent",
    )

    assert result["status"] == "failed"
    assert "Invalid stage_id or close_date format" in result["reason"]
    assert result["requires_approval"] is False


@pytest.mark.asyncio
async def test_lifecycle_close_rejects_invalid_close_date_before_approval():
    org_id = uuid4()

    """Do not ask approval for a close action with an invalid actual close date."""
    context = AgentContext(organisation_id=org_id, deal_id=uuid4())
    tool = create_update_deal_lifecycle(context, lambda: None)

    result = await tool(
        action="close_won",
        close_date="not-a-date",
        directly_requested=False,
        source="agent",
    )

    assert result["status"] == "failed"
    assert "Invalid stage_id or close_date format" in result["reason"]
    assert result["requires_approval"] is False


@pytest.mark.asyncio
async def test_lifecycle_close_won_approval_payload_clears_closed_reason():
    org_id = uuid4()

    """Do not ask approval for won close reasons; closed_reason is lost-only."""
    context = AgentContext(organisation_id=org_id, deal_id=uuid4())
    tool = create_update_deal_lifecycle(context, lambda: None)

    result = await tool(
        action="close_won",
        closed_reason="Contract signed",
        directly_requested=True,
        source="chat",
    )

    assert result["status"] == "approval_required"
    assert result["approval_payload"]["closed_reason"] is None


@pytest.mark.asyncio
async def test_lifecycle_move_stage_returns_refreshed_deal_context():
    org_id = uuid4()
    deal_id = uuid4()
    target_stage_id = uuid4()

    """Applied lifecycle updates should return enough context to refresh the deal row."""
    context = AgentContext(organisation_id=org_id, deal_id=deal_id, user_id=uuid4())
    tool = create_update_deal_lifecycle(context, lambda: None)
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    deal = SimpleNamespace(
        deal_id=deal_id,
        amount=12000.0,
        predicted_amount=9000.0,
        currency="USD",
        deal_status="open",
        stage_id=target_stage_id,
        stage_name="Proposal",
        pipeline_id=uuid4(),
        pipeline_name="Sales",
    )

    async def single_session(session):
        yield session

    with (
        patch(
            "src.agents.tools.crm.update_deal_lifecycle.acquire_master_async_session",
            return_value=single_session(MagicMock()),
        ),
        patch(
            "src.agents.tools.crm.update_deal_lifecycle.acquire_org_async_session",
            return_value=single_session(org_session),
        ),
        patch(
            "src.agents.tools.crm.update_deal_lifecycle.classify_deal_update_policy",
            return_value=SimpleNamespace(status="applied", requires_approval=False, reason="ok"),
        ),
        patch("src.agents.tools.crm.update_deal_lifecycle.DealWriteService") as service_cls,
    ):
        service = MagicMock()
        service.move_stage = AsyncMock(return_value=deal)
        service_cls.return_value = service

        result = await tool(
            action="move_stage",
            stage_id=str(target_stage_id),
            directly_requested=True,
            source="chat",
        )

    service.move_stage.assert_awaited_once_with(deal_id, target_stage_id)
    assert result["status"] == "applied"
    assert result["amount"] == 12000.0
    assert result["predicted_amount"] == 9000.0
    assert result["currency"] == "USD"
    assert result["deal_status"] == "open"
    assert result["stage_id"] == str(target_stage_id)
    assert result["stage_name"] == "Proposal"
    assert result["pipeline_id"] == str(deal.pipeline_id)
    assert result["pipeline_name"] == "Sales"


@pytest.mark.asyncio
async def test_account_tool_rejects_empty_payload():
    org_id = uuid4()

    """Return failed status when an account update tool call has no payload."""
    context = AgentContext(organisation_id=org_id, deal_id=uuid4())
    tool = create_update_deal_account(context, lambda: None)

    result = await tool(account={}, directly_requested=True, source="chat")

    assert result["status"] == "failed"
    assert "At least one account field" in result["reason"]


@pytest.mark.asyncio
async def test_account_tool_rejects_invalid_payload_before_approval():
    org_id = uuid4()

    """Validate account field values before approval so bad data is not reviewable."""
    context = AgentContext(organisation_id=org_id, deal_id=uuid4())
    tool = create_update_deal_account(context, lambda: None)

    result = await tool(account={"website": 123}, directly_requested=False, source="agent")

    assert result["status"] == "failed"
    assert "Input should be a valid string" in result["reason"]
    assert result["requires_approval"] is False


@pytest.mark.asyncio
async def test_account_tool_ignores_inferred_nulls_when_other_fields_need_approval():
    org_id = uuid4()

    """Ignore inferred empty account values while still reviewing real changes."""
    context = AgentContext(organisation_id=org_id, deal_id=uuid4())
    tool = create_update_deal_account(context, lambda: None)

    result = await tool(
        account={"website": None, "name": "Acme Corp"},
        directly_requested=False,
        source="agent",
    )

    assert result["status"] == "approval_required"
    assert result["approval_payload"]["account"] == {"name": "Acme Corp"}
    assert result["requires_approval"] is True


@pytest.mark.asyncio
async def test_account_tool_applies_normalized_payload():
    org_id = uuid4()
    deal_id = uuid4()

    """Apply account updates using the same normalized payload as the public API."""
    context = AgentContext(organisation_id=org_id, deal_id=deal_id, user_id=uuid4())
    tool = create_update_deal_account(context, lambda: None)
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    deal = SimpleNamespace(
        deal_id=deal_id,
        amount=12000.0,
        predicted_amount=6000.0,
        currency="USD",
        deal_status="open",
        stage_id=uuid4(),
        stage_name="Negotiation",
        pipeline_id=uuid4(),
        pipeline_name="Sales",
    )

    async def single_session(session):
        yield session

    with (
        patch(
            "src.agents.tools.crm.update_deal_account.acquire_master_async_session",
            return_value=single_session(MagicMock()),
        ),
        patch(
            "src.agents.tools.crm.update_deal_account.acquire_org_async_session",
            return_value=single_session(org_session),
        ),
        patch("src.agents.tools.crm.update_deal_account.DealWriteService") as service_cls,
    ):
        service = MagicMock()
        service.update_account = AsyncMock(return_value=deal)
        service_cls.return_value = service

        result = await tool(
            account={"name": "  Acme Corp  ", "website": None},
            directly_requested=True,
            source="chat",
        )

    service.update_account.assert_awaited_once_with(
        deal_id,
        {"name": "Acme Corp", "website": None},
    )
    assert result["status"] == "applied"
    assert result["applied_fields"] == {"name": "Acme Corp", "website": None}
    assert result["amount"] == 12000.0
    assert result["predicted_amount"] == 6000.0
    assert result["currency"] == "USD"
    assert result["deal_status"] == "open"
    assert result["stage_id"] == str(deal.stage_id)
    assert result["stage_name"] == "Negotiation"
    assert result["pipeline_id"] == str(deal.pipeline_id)
    assert result["pipeline_name"] == "Sales"


@pytest.mark.asyncio
async def test_update_deal_fields_applies_direct_clear_payload():
    org_id = uuid4()
    deal_id = uuid4()

    """Directly requested scalar clears should follow the public PATCH contract."""
    context = AgentContext(organisation_id=org_id, deal_id=deal_id, user_id=uuid4())
    tool = create_update_deal_fields(context, lambda: None)
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    deal = SimpleNamespace(
        deal_id=deal_id,
        amount=12000.0,
        predicted_amount=6000.0,
        currency="USD",
        deal_status="open",
        stage_id=uuid4(),
        stage_name="Negotiation",
        pipeline_id=uuid4(),
        pipeline_name="Sales",
    )

    async def single_session(session):
        yield session

    with (
        patch(
            "src.agents.tools.crm.update_deal_fields.acquire_master_async_session",
            return_value=single_session(MagicMock()),
        ),
        patch(
            "src.agents.tools.crm.update_deal_fields.acquire_org_async_session",
            return_value=single_session(org_session),
        ),
        patch("src.agents.tools.crm.update_deal_fields.DealWriteService") as service_cls,
    ):
        service = MagicMock()
        service.update_deal = AsyncMock(return_value=deal)
        service_cls.return_value = service

        result = await tool(
            fields={"description": None},
            directly_requested=True,
            source="chat",
        )

    service.update_deal.assert_awaited_once_with(deal_id, {"description": None})
    assert result["status"] == "applied"
    assert result["applied_fields"] == {"description": None}
    assert result["amount"] == 12000.0
    assert result["predicted_amount"] == 6000.0
    assert result["currency"] == "USD"
    assert result["deal_status"] == "open"
    assert result["stage_id"] == str(deal.stage_id)
    assert result["stage_name"] == "Negotiation"
    assert result["pipeline_id"] == str(deal.pipeline_id)
    assert result["pipeline_name"] == "Sales"


@pytest.mark.asyncio
async def test_update_deal_fields_ignores_inferred_nulls_when_other_fields_need_approval():
    org_id = uuid4()

    """Ignore inferred empty field values while still reviewing real changes."""
    context = AgentContext(organisation_id=org_id, deal_id=uuid4())
    tool = create_update_deal_fields(context, lambda: None)

    result = await tool(
        fields={"description": None, "amount": 12000},
        directly_requested=False,
        source="agent",
    )

    assert result["status"] == "approval_required"
    assert result["approval_payload"]["fields"] == {"amount": 12000.0}
    assert result["requires_approval"] is True


def test_account_tool_supported_fields_match_public_account_update_contract():
    """Agent account edits should not lag behind the public account action schema."""
    expected_fields = set(DealAccountUpdateRequest.model_fields) - {"company_id"}

    assert expected_fields == ALLOWED_ACCOUNT_FIELDS
    assert expected_fields == ACCOUNT_FIELDS


def test_field_tool_supported_fields_match_public_inline_update_contract():
    """Agent direct deal edits should match public PATCH fields except custom fields."""
    expected_fields = set(DealUpdateRequest.model_fields) - {"custom_properties", "custom_columns"}

    assert expected_fields == ALLOWED_DEAL_FIELDS
    assert expected_fields == SAFE_DIRECT_FIELDS


def test_update_deal_fields_normalizes_public_update_values():
    """Normalize agent field values through the same schema contract as public PATCH."""
    normalized = _normalize_deal_fields(
        {
            "name": "  Enterprise renewal  ",
            "currency": "usd",
            "expected_close_date": "2026-05-01",
            "source_created_at": "2026-04-01T10:30:00Z",
        }
    )

    assert normalized["name"] == "Enterprise renewal"
    assert normalized["currency"] == "USD"
    assert normalized["expected_close_date"] == datetime(2026, 5, 1)
    assert normalized["source_created_at"].isoformat() == "2026-04-01T10:30:00+00:00"


@pytest.mark.asyncio
async def test_update_deal_fields_rejects_actual_close_date():
    org_id = uuid4()

    """Reject actual close date in scalar fields; lifecycle close actions own that value."""
    context = AgentContext(organisation_id=org_id, deal_id=uuid4())
    tool = create_update_deal_fields(context, lambda: None)

    result = await tool(fields={"close_date": "2026-05-01"}, directly_requested=True, source="chat")

    assert result["status"] == "skipped"
    assert "not supported" in result["reason"]
