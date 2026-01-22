"""CRM settings and dropdown options API handler.

Provides lightweight endpoints for populating deal form dropdowns.
These return static/configurable options for currencies, deal types, etc.

Endpoints:
- GET /settings/currencies - List available currencies
- GET /settings/deal-options - All dropdown options in single call
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
)
from src.api.dependencies.rate_limit import rate_limit_user_read
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_settings_models import (
    CurrencyListResponse,
    CurrencyOption,
    DealFormOptionsResponse,
    DropdownOption,
    DropdownOptionsResponse,
)
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)

settings_router = APIRouter()

# =============================================================================
# Static Data Definitions
# =============================================================================

# Available currencies (currently USD and INR as requested)
# Can be expanded or made configurable per org in the future
AVAILABLE_CURRENCIES: list[CurrencyOption] = [
    CurrencyOption(code="USD", name="US Dollar", symbol="$"),
    CurrencyOption(code="INR", name="Indian Rupee", symbol="₹"),
]

DEFAULT_CURRENCY = "USD"

# Deal type options
DEAL_TYPE_OPTIONS: list[DropdownOption] = [
    DropdownOption(
        value="New Business", label="New Business", description="First-time customer deal"
    ),
    DropdownOption(value="Renewal", label="Renewal", description="Existing customer renewal"),
    DropdownOption(value="Expansion", label="Expansion", description="Expanding existing contract"),
    DropdownOption(value="Upsell", label="Upsell", description="Upgrading to higher tier"),
]

# Priority options
PRIORITY_OPTIONS: list[DropdownOption] = [
    DropdownOption(value="High", label="High", description="Urgent, needs immediate attention"),
    DropdownOption(value="Medium", label="Medium", description="Normal priority"),
    DropdownOption(value="Low", label="Low", description="Can be addressed later"),
]

# Lead source options
LEAD_SOURCE_OPTIONS: list[DropdownOption] = [
    DropdownOption(value="Inbound", label="Inbound", description="Customer reached out"),
    DropdownOption(value="Outbound", label="Outbound", description="Sales outreach"),
    DropdownOption(value="Referral", label="Referral", description="Referred by existing customer"),
    DropdownOption(value="Partner", label="Partner", description="Partner referral"),
    DropdownOption(value="Event", label="Event", description="Trade show or conference"),
    DropdownOption(value="Website", label="Website", description="Website form submission"),
    DropdownOption(value="Other", label="Other", description="Other source"),
]

# Forecast category options (for Phase 3)
FORECAST_CATEGORY_OPTIONS: list[DropdownOption] = [
    DropdownOption(
        value="Pipeline", label="Pipeline", description="In pipeline, not yet qualified"
    ),
    DropdownOption(value="Best Case", label="Best Case", description="Possible but not committed"),
    DropdownOption(value="Commit", label="Commit", description="Highly likely to close"),
    DropdownOption(value="Closed", label="Closed", description="Deal is closed"),
]


# =============================================================================
# API Endpoints
# =============================================================================


@settings_router.get(
    "/settings/currencies",
    response_model=GenericResponse[CurrencyListResponse],
    summary="List available currencies for deal amount",
)
async def list_currencies(
    _user: UserIDs = Depends(get_current_user),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[CurrencyListResponse]:
    """List available currencies for deal forms.

    Returns a list of supported currencies with their codes, names, and symbols.
    Currently supports USD and INR. Can be expanded based on org settings.
    """
    payload = CurrencyListResponse(
        items=AVAILABLE_CURRENCIES,
        default_code=DEFAULT_CURRENCY,
    )
    return Response.success_response(message="Currencies retrieved successfully", data=payload)


@settings_router.get(
    "/settings/deal-options",
    response_model=GenericResponse[DealFormOptionsResponse],
    summary="Get all dropdown options for deal forms",
)
async def get_deal_form_options(
    _user: UserIDs = Depends(get_current_user),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[DealFormOptionsResponse]:
    """Get all dropdown options for deal create/edit forms.

    Returns currencies, deal types, priorities, lead sources, and forecast
    categories in a single API call to minimize network requests.

    This endpoint is optimized for caching - the options rarely change.
    """
    payload = DealFormOptionsResponse(
        currencies=CurrencyListResponse(
            items=AVAILABLE_CURRENCIES,
            default_code=DEFAULT_CURRENCY,
        ),
        deal_types=DropdownOptionsResponse(
            items=DEAL_TYPE_OPTIONS,
            default_value=None,
        ),
        priorities=DropdownOptionsResponse(
            items=PRIORITY_OPTIONS,
            default_value="Medium",
        ),
        lead_sources=DropdownOptionsResponse(
            items=LEAD_SOURCE_OPTIONS,
            default_value=None,
        ),
        forecast_categories=DropdownOptionsResponse(
            items=FORECAST_CATEGORY_OPTIONS,
            default_value="Pipeline",
        ),
    )
    return Response.success_response(
        message="Deal form options retrieved successfully", data=payload
    )
