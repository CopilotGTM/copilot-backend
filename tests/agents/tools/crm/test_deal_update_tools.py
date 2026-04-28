"""Regression coverage for account, field, and lifecycle deal-update tools."""

from uuid import uuid4

import pytest
from src.agents.context import AgentContext
from src.agents.tools.crm.update_deal_account import create_update_deal_account
from src.agents.tools.crm.update_deal_fields import create_update_deal_fields
from src.agents.tools.crm.update_deal_lifecycle import create_update_deal_lifecycle


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
async def test_account_tool_rejects_empty_payload():
    org_id = uuid4()

    """Return failed status when an account update tool call has no payload."""
    context = AgentContext(organisation_id=org_id, deal_id=uuid4())
    tool = create_update_deal_account(context, lambda: None)

    result = await tool(account={}, directly_requested=True, source="chat")

    assert result["status"] == "failed"
    assert "At least one account field" in result["reason"]


@pytest.mark.asyncio
async def test_update_deal_fields_rejects_actual_close_date():
    org_id = uuid4()

    """Reject actual close date in scalar fields; lifecycle close actions own that value."""
    context = AgentContext(organisation_id=org_id, deal_id=uuid4())
    tool = create_update_deal_fields(context, lambda: None)

    result = await tool(fields={"close_date": "2026-05-01"}, directly_requested=True, source="chat")

    assert result["status"] == "skipped"
    assert "not supported" in result["reason"]


@pytest.mark.asyncio
async def test_account_tool_rejects_name_and_country_fields():
    org_id = uuid4()

    """Reject account identity/location fields that are outside the first slice."""
    context = AgentContext(organisation_id=org_id, deal_id=uuid4())
    tool = create_update_deal_account(context, lambda: None)

    result = await tool(
        account={"name": "Acme", "country": "US"}, directly_requested=True, source="chat"
    )

    assert result["status"] == "skipped"
    assert "not supported" in result["reason"]
