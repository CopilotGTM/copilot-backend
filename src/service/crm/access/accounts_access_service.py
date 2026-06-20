"""Deprecated compatibility facade for CRM account services.

Kept as a short-lived import compatibility surface while account reads and
writes move to the vertical `src.service.crm.accounts` module. New production
callers should import `AccountReadService` or `AccountWriteService` directly.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.utils.logger import get_logger
from src.service.crm.accounts.contracts import (
    AccountReadServiceError,
    AccountWriteServiceError,
)
from src.service.crm.accounts.read_service import AccountReadService
from src.service.crm.accounts.write_service import AccountWriteService

logger = get_logger("accounts_access_service")

# Keep the historical exception name importable during rollout. Write errors are
# translated below so old callers still receive the same shaped exception.
AccountsAccessServiceError = AccountReadServiceError


class AccountsAccessService:
    """Compatibility layer delegating to account read/write services."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        """Create read and write delegates over the same org-scoped session."""
        self.org_session = org_session
        self.organisation_id = organisation_id
        self._reader = AccountReadService(org_session, organisation_id)
        self._writer = AccountWriteService(org_session, organisation_id)

    async def close(self) -> None:
        """Close the shared session for legacy dependency callers."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("AccountsAccessService close exception", exc_info=True)

    @staticmethod
    def _normalize_domain(domain: str | None) -> str | None:
        return AccountReadService._normalize_domain(domain)

    async def suggest_account_by_domain(self, email_domain: str) -> dict | None:
        """Delegate domain suggestions for legacy dropdown callers."""
        return await self._reader.suggest_account_by_domain(email_domain)

    async def find_accounts_by_domain(self, domain: str) -> list[CRMCompany]:
        """Delegate tenant-scoped exact domain lookup for legacy callers."""
        return await self._reader.find_accounts_by_domain(domain)

    async def find_accounts_by_name(self, name: str) -> list[CRMCompany]:
        """Delegate tenant-scoped exact name lookup for legacy callers."""
        return await self._reader.find_accounts_by_name(name)

    async def list_accounts(
        self,
        *,
        page: int = 1,
        limit: int = 25,
        q: str | None = None,
        industry: str | None = None,
    ) -> tuple[list[dict], int]:
        """Delegate account table pagination while preserving the old facade API."""
        return await self._reader.list_accounts(page=page, limit=limit, q=q, industry=industry)

    async def get_account_detail(self, company_id: UUID) -> dict:
        """Delegate account detail lookup for callers not yet migrated."""
        return await self._reader.get_account_detail(company_id)

    async def list_account_contacts(
        self, company_id: UUID, *, limit: int = 25
    ) -> list[dict[str, Any]]:
        """Delegate contact summary rows for legacy account-detail callers."""
        return await self._reader.list_account_contacts(company_id, limit=limit)

    async def get_account_contacts(
        self, company_id: UUID, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Delegate contact rows with deal counts for legacy callers."""
        return await self._reader.get_account_contacts(company_id, limit=limit)

    async def get_account_deals(self, company_id: UUID) -> list[CRMDeal]:
        """Delegate account deal lookup for callers not yet migrated."""
        return await self._reader.get_account_deals(company_id)

    async def create_account(self, name: str | None, **kwargs: Any) -> CRMCompany:
        """Create an account through AccountWriteService for legacy callers."""
        try:
            result = await self._writer.create_account(name=name, **kwargs)
        except AccountWriteServiceError as exc:
            # Older callers catch AccountsAccessServiceError, so translate write
            # errors instead of leaking the new vertical-service exception type.
            raise AccountsAccessServiceError(
                exc.message, status_code=exc.status_code, data=exc.data
            ) from exc
        return result.account

    async def update_account(self, company_id: UUID, updates: dict[str, Any]) -> CRMCompany:
        """Update an account through AccountWriteService for legacy callers."""
        try:
            return await self._writer.update_account(company_id, updates)
        except AccountWriteServiceError as exc:
            raise AccountsAccessServiceError(
                exc.message, status_code=exc.status_code, data=exc.data
            ) from exc
