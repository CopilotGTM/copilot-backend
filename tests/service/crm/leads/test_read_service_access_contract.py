"""Regression tests for the lead read service and access-policy contract."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.service.crm.leads.read_service import LeadReadService


@pytest.mark.asyncio
async def test_get_lead_or_404_accepts_the_single_lead_returned_by_policy() -> None:
    """Lead loading must match LeadAccessPolicy.require_lead's return value."""
    lead_id = uuid4()
    lead = SimpleNamespace(lead_id=lead_id)
    service = LeadReadService(None, uuid4())  # type: ignore[arg-type]
    service._access = SimpleNamespace(  # type: ignore[assignment]
        require_lead=AsyncMock(return_value=lead),
    )

    result = await service._get_lead_or_404(lead_id)

    assert result is lead
