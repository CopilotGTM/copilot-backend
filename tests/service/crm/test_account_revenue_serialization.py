"""Regression tests for database-backed account revenue serialization."""

from __future__ import annotations

import warnings
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.service.crm.accounts.read_service import AccountReadService


def _database_company() -> CRMCompany:
    """Build the runtime shape produced when SQLAlchemy hydrates NUMERIC revenue."""
    company = CRMCompany(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        company_id=uuid4(),
    )
    # SQLAlchemy populates mapped attributes after Pydantic validation, so mimic
    # that path rather than allowing the constructor to coerce Decimal first.
    company.__dict__["annual_revenue"] = Decimal("5854000.00")
    return company


def test_crm_company_serializes_database_decimal_revenue_as_float() -> None:
    """All CRMCompany serializers must emit warning-free float revenue."""
    company = _database_company()

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        payload = company.model_dump(include={"annual_revenue"})

    assert payload["annual_revenue"] == 5854000.0
    assert type(payload["annual_revenue"]) is float
    assert captured == []


@pytest.mark.asyncio
async def test_list_accounts_serializes_database_decimal_revenue_without_warning() -> None:
    """The reported accounts endpoint must not log one warning per revenue value."""
    company = _database_company()
    count_result = MagicMock()
    count_result.scalar.return_value = 1
    rows_result = MagicMock()
    rows_result.all.return_value = [(company, 0, 0, Decimal("0.00"))]
    session = AsyncMock()
    session.execute.side_effect = [count_result, rows_result]

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        rows, total_count = await AccountReadService(
            session,
            company.organisation_id,
        ).list_accounts()

    assert total_count == 1
    assert type(rows[0]["annual_revenue"]) is float
    assert captured == []
