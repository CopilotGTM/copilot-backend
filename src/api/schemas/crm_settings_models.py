"""CRM settings and options response models.

Provides dropdown options for deal forms (currencies, deal types, priorities, lead sources).
These are lightweight endpoints to support frontend dropdown population.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CurrencyOption(BaseModel):
    """Currency option for deal amount selection."""

    code: str = Field(..., description="ISO 4217 currency code (e.g., USD, INR)")
    name: str = Field(..., description="Currency display name")
    symbol: str = Field(..., description="Currency symbol (e.g., $, ₹)")

    model_config = {"from_attributes": True}


class CurrencyListResponse(BaseModel):
    """List of available currencies."""

    items: list[CurrencyOption]
    default_code: str = Field(default="USD", description="Default currency code")


class DropdownOption(BaseModel):
    """Generic dropdown option for deal form fields."""

    value: str = Field(..., description="Option value to store")
    label: str = Field(..., description="Option display label")
    description: str | None = Field(default=None, description="Optional description")

    model_config = {"from_attributes": True}


class DropdownOptionsResponse(BaseModel):
    """List of dropdown options for a field."""

    items: list[DropdownOption]
    default_value: str | None = Field(default=None, description="Default selection")


class DealFormOptionsResponse(BaseModel):
    """All dropdown options for deal create/edit forms.

    This endpoint returns all options in a single call to minimize
    API requests when opening the deal form.
    """

    currencies: CurrencyListResponse
    deal_types: DropdownOptionsResponse
    priorities: DropdownOptionsResponse
    lead_sources: DropdownOptionsResponse
    forecast_categories: DropdownOptionsResponse
