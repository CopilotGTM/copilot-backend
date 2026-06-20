"""Compatibility facade for legacy deal read imports."""

from __future__ import annotations

from src.service.crm.deals.contracts import DealReadServiceError as DealsAccessServiceError
from src.service.crm.deals.read_service import DealReadService as DealsAccessService
from src.service.crm.deals.read_service import (
    extract_deal_health,
    extract_deal_health_reason,
)

__all__ = [
    "DealsAccessService",
    "DealsAccessServiceError",
    "extract_deal_health",
    "extract_deal_health_reason",
]
