"""Authentication service for private lead ingestion key flow."""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime
from typing import cast

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.dependencies.rate_limit import enforce_private_lead_ingestion_rate_limit
from src.domain.models.master_tables import (
    PublicIngressKey,
    PublicIngressKeyStatus,
    PublicIngressKeyType,
    PublicIngressResourceType,
)
from src.service.crm.leads.ingestion_key_service import LeadIngestionKeyService


class PrivateLeadIngestionAuthError(Exception):
    """External private lead ingestion authentication error."""

    def __init__(self, code: str, message: str, status_code: int = 401, field: str | None = None):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.field = field
        super().__init__(message)


class PrivateLeadIngestionAuthService:
    """Authenticate private lead ingestion key id and bearer secret."""

    def __init__(self, master_session: AsyncSession):
        self.master_session = master_session

    async def authenticate(
        self,
        *,
        key_id: str | None,
        authorization: str | None,
        client_ip: str,
        resource_type: PublicIngressResourceType = PublicIngressResourceType.LEAD_INGESTION,
    ) -> PublicIngressKey:
        """Validate key id, secret, scope, IP policy, and rate-limit policy."""
        if not key_id or not key_id.startswith("skid_"):
            raise PrivateLeadIngestionAuthError("missing_key_id", "Missing ingestion key id", 401)
        secret = self._bearer_secret(authorization)
        key = await self._load_key(key_id, resource_type=resource_type)
        self._validate_key_policy(key, resource_type=resource_type)
        if not await LeadIngestionKeyService.verify_secret(secret, key.secret_hash):
            raise PrivateLeadIngestionAuthError(
                "invalid_credentials",
                "Invalid ingestion credentials",
                401,
            )
        self._validate_allowed_ip(key.allowed_ips or [], client_ip)
        try:
            enforce_private_lead_ingestion_rate_limit(
                key.public_key or key_id, key.rate_limit_policy
            )
        except HTTPException as exc:
            raise PrivateLeadIngestionAuthError(
                "rate_limited",
                exc.detail if isinstance(exc.detail, str) else "Too many requests",
                exc.status_code,
            ) from exc
        key.last_used_at = datetime.now(UTC)
        self.master_session.add(key)
        await self.master_session.commit()
        await self.master_session.refresh(key)
        return key

    async def _load_key(
        self,
        key_id: str,
        *,
        resource_type: PublicIngressResourceType = PublicIngressResourceType.LEAD_INGESTION,
    ) -> PublicIngressKey:
        """Load a private ingress key row for validation and metadata update."""
        stmt = select(PublicIngressKey).where(
            col(PublicIngressKey.public_key) == key_id,
            col(PublicIngressKey.key_type) == PublicIngressKeyType.PRIVATE_API.value,
            col(PublicIngressKey.resource_type) == resource_type.value,
        )
        key = (await self.master_session.execute(stmt)).scalars().first()
        if not key:
            raise PrivateLeadIngestionAuthError(
                "invalid_credentials",
                "Invalid ingestion credentials",
                401,
            )
        return cast(PublicIngressKey, key)

    @staticmethod
    def _bearer_secret(authorization: str | None) -> str:
        """Extract bearer secret value from Authorization header."""
        if not authorization or not authorization.lower().startswith("bearer "):
            raise PrivateLeadIngestionAuthError(
                "missing_secret",
                "Missing ingestion bearer secret",
                401,
            )
        secret = authorization[7:].strip()
        if not secret:
            raise PrivateLeadIngestionAuthError(
                "missing_secret",
                "Missing ingestion bearer secret",
                401,
            )
        return secret

    @staticmethod
    def _validate_key_policy(
        key: PublicIngressKey,
        *,
        resource_type: PublicIngressResourceType = PublicIngressResourceType.LEAD_INGESTION,
    ) -> None:
        """Enforce scope, status, and expiry constraints for an auth key."""
        if (
            resource_type == PublicIngressResourceType.LEAD_INGESTION
            and key.resource_id != key.organisation_id
        ):
            raise PrivateLeadIngestionAuthError("invalid_key_scope", "Invalid ingestion key", 401)
        if key.status != PublicIngressKeyStatus.ACTIVE.value:
            raise PrivateLeadIngestionAuthError("key_inactive", "Ingestion key is not active", 403)
        if key.expires_at and key.expires_at <= datetime.now(UTC):
            raise PrivateLeadIngestionAuthError("key_expired", "Ingestion key is expired", 403)

    @staticmethod
    def _validate_allowed_ip(allowed_ips: list[str], client_ip: str) -> None:
        """Enforce allow-listed IP/network policy for ingestion key usage."""
        if not allowed_ips:
            return
        try:
            ip = ipaddress.ip_address(client_ip)
        except ValueError as exc:
            raise PrivateLeadIngestionAuthError(
                "ip_not_allowed",
                "Client IP is not allowed for this ingestion key",
                403,
            ) from exc
        for allowed in allowed_ips:
            try:
                if "/" in allowed:
                    if ip in ipaddress.ip_network(allowed, strict=False):
                        return
                elif ip == ipaddress.ip_address(allowed):
                    return
            except ValueError:
                continue
        raise PrivateLeadIngestionAuthError(
            "ip_not_allowed",
            "Client IP is not allowed for this ingestion key",
            403,
        )
