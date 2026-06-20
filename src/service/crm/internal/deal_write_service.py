"""Compatibility facade for legacy internal deal write imports."""

from __future__ import annotations

from src.service.crm.deals.contracts import DealWriteServiceError
from src.service.crm.deals.write_service import DealWriteService

__all__ = ["DealWriteService", "DealWriteServiceError"]
