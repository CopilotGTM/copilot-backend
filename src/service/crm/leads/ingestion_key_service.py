"""Service helpers for private lead-ingestion API key lifecycle."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.schemas.private_lead_ingestion_models import (
    CreateLeadIngestionKeyRequest,
    LeadIngestionKeyItem,
)
from src.domain.models.master_tables import (
    PublicIngressKey,
    PublicIngressKeyStatus,
    PublicIngressKeyType,
    PublicIngressResourceType,
)
from src.infra.config.configs import security_secrets_config

_KEY_ID_PREFIX = "skid_"
_SECRET_PREFIX = "cgtm_live_"  # pragma: allowlist secret
_HASH_PREFIX = "hmac_sha256$"


class LeadIngestionKeyServiceError(Exception):
    """Private lead ingestion key management error."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)


class LeadIngestionKeyService:
    """Authenticated CRUD for organisation-level private lead ingestion keys."""

    def __init__(
        self,
        master_session: AsyncSession,
        organisation_id: UUID,
        actor_user_id: UUID | None,
    ):
        self.master_session = master_session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id

    async def close(self) -> None:
        """Close the master DB session bound to this service."""
        await self.master_session.close()

    async def create_key(
        self,
        request: CreateLeadIngestionKeyRequest,
        *,
        resource_type: PublicIngressResourceType = PublicIngressResourceType.LEAD_INGESTION,
        resource_id: UUID | None = None,
        metadata: dict[str, str | None] | None = None,
    ) -> tuple[PublicIngressKey, str]:
        """Create an active private ingestion key and return both record and plaintext secret."""
        key_id = await self._generate_unique_key_id()
        secret = self.generate_secret()
        secret_hash = await self.hash_secret(secret)
        key_metadata = {
            "name": request.name.strip(),
            "created_by_user_id": str(self.actor_user_id) if self.actor_user_id else None,
        }
        if metadata:
            key_metadata.update(metadata)
        key = PublicIngressKey(
            organisation_id=self.organisation_id,
            key_type=PublicIngressKeyType.PRIVATE_API,
            resource_type=resource_type,
            resource_id=resource_id or self.organisation_id,
            public_key=key_id,
            secret_hash=secret_hash,
            status=PublicIngressKeyStatus.ACTIVE,
            allowed_ips=request.allowed_ips,
            rate_limit_policy=request.rate_limit_policy,
            expires_at=request.expires_at,
            key_metadata=key_metadata,
        )
        self.master_session.add(key)
        await self.master_session.commit()
        await self.master_session.refresh(key)
        return key, secret

    async def list_keys(
        self,
        *,
        resource_type: PublicIngressResourceType = PublicIngressResourceType.LEAD_INGESTION,
        resource_id: UUID | None = None,
    ) -> list[LeadIngestionKeyItem]:
        """List private ingestion keys for the current organisation."""
        stmt = (
            select(PublicIngressKey)
            .where(
                col(PublicIngressKey.organisation_id) == self.organisation_id,
                col(PublicIngressKey.key_type) == PublicIngressKeyType.PRIVATE_API.value,
                col(PublicIngressKey.resource_type) == resource_type.value,
            )
            .order_by(col(PublicIngressKey.created_at).desc())
        )
        if resource_id is not None:
            stmt = stmt.where(col(PublicIngressKey.resource_id) == resource_id)
        keys = list((await self.master_session.execute(stmt)).scalars().all())
        return [self.to_item(key) for key in keys]

    async def update_status(
        self,
        key_id: str,
        status: PublicIngressKeyStatus,
        *,
        resource_type: PublicIngressResourceType = PublicIngressResourceType.LEAD_INGESTION,
        resource_id: UUID | None = None,
    ) -> PublicIngressKey:
        """Update key status and persist revocation timestamps for transition metadata."""
        key = await self._get_org_key_or_404(
            key_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        current_status = PublicIngressKeyStatus(key.status)
        if (
            current_status == PublicIngressKeyStatus.REVOKED
            and status != PublicIngressKeyStatus.REVOKED
        ):
            raise LeadIngestionKeyServiceError("Revoked ingestion keys cannot be reactivated", 409)
        if (
            status == PublicIngressKeyStatus.REVOKED
            and current_status != PublicIngressKeyStatus.REVOKED
        ):
            key.revoked_at = datetime.now(UTC)
        key.status = status
        self.master_session.add(key)
        await self.master_session.commit()
        await self.master_session.refresh(key)
        return key

    async def _get_org_key_or_404(
        self,
        key_id: str,
        *,
        resource_type: PublicIngressResourceType = PublicIngressResourceType.LEAD_INGESTION,
        resource_id: UUID | None = None,
    ) -> PublicIngressKey:
        stmt = select(PublicIngressKey).where(
            col(PublicIngressKey.organisation_id) == self.organisation_id,
            col(PublicIngressKey.public_key) == key_id,
            col(PublicIngressKey.key_type) == PublicIngressKeyType.PRIVATE_API.value,
            col(PublicIngressKey.resource_type) == resource_type.value,
        )
        if resource_id is not None:
            stmt = stmt.where(col(PublicIngressKey.resource_id) == resource_id)
        key = (await self.master_session.execute(stmt)).scalars().first()
        if not key:
            raise LeadIngestionKeyServiceError("Lead ingestion key not found", 404)
        return key

    async def _generate_unique_key_id(self) -> str:
        """Generate a non-conflicting `skid_` key id under current org keys."""
        for _ in range(10):
            key_id = (
                _KEY_ID_PREFIX + secrets.token_urlsafe(18).replace("-", "").replace("_", "")[:24]
            )
            exists = (
                (
                    await self.master_session.execute(
                        select(PublicIngressKey).where(col(PublicIngressKey.public_key) == key_id)
                    )
                )
                .scalars()
                .first()
            )
            if not exists:
                return key_id
        raise LeadIngestionKeyServiceError("Could not generate a unique ingestion key", 500)

    @staticmethod
    def generate_secret() -> str:
        """Generate a random private secret in the shared private-ingestion format."""
        return _SECRET_PREFIX + secrets.token_urlsafe(32)

    @staticmethod
    async def hash_secret(secret: str) -> str:
        """Hash a shared secret with rotating pepper for deterministic validation."""
        pepper = os.getenv("LEAD_INGESTION_SECRET_PEPPER", "").strip()
        if not pepper:
            pepper = await security_secrets_config.get_oauth_state_secret()
        digest = hmac.new(pepper.encode(), secret.encode(), hashlib.sha256).hexdigest()
        return f"{_HASH_PREFIX}{digest}"

    @staticmethod
    async def verify_secret(secret: str, stored_hash: str | None) -> bool:
        """Verify a candidate secret against a stored HMAC-SHA256 digest."""
        if not secret or not stored_hash:
            return False
        expected = await LeadIngestionKeyService.hash_secret(secret)
        return hmac.compare_digest(expected, stored_hash)

    @staticmethod
    def to_item(key: PublicIngressKey) -> LeadIngestionKeyItem:
        """Convert a DB key model into an authenticated API response shape."""
        metadata = key.key_metadata or {}
        name = metadata.get("name") if isinstance(metadata.get("name"), str) else None
        return LeadIngestionKeyItem(
            key_id=key.public_key or str(key.key_id),
            name=name,
            status=PublicIngressKeyStatus(key.status),
            allowed_ips=key.allowed_ips or [],
            rate_limit_policy=key.rate_limit_policy or {},
            expires_at=key.expires_at,
            last_used_at=key.last_used_at,
            revoked_at=key.revoked_at,
            created_at=key.created_at,
            updated_at=key.updated_at,
        )
