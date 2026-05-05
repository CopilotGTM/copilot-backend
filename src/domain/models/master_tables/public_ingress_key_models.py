"""Master-schema ingress key models for external entrypoints."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from .base_mixins import TimestampMixin


class PublicIngressKeyType(str, Enum):
    """External entrypoint auth/use-case type."""

    PUBLIC_FORM = "public_form"
    PRIVATE_API = "private_api"
    WEBHOOK = "webhook"
    EMBED = "embed"
    TRACKING = "tracking"


class PublicIngressResourceType(str, Enum):
    """Org-schema resource exposed through an ingress key."""

    LEAD_FORM = "lead_form"
    LEAD_INGESTION = "lead_ingestion"
    WEBHOOK_ENDPOINT = "webhook_endpoint"


class PublicIngressKeyStatus(str, Enum):
    """Lifecycle state for an external ingress key."""

    ACTIVE = "active"
    PAUSED = "paused"
    REVOKED = "revoked"


class PublicIngressKey(TimestampMixin, table=True):
    """Master-schema registry for public/API keys that resolve to org resources.

    This table intentionally knows nothing about form internals. It maps an
    external key to an organisation and an org-schema resource, then carries the
    security policy for that external entrypoint.
    """

    __tablename__ = "public_ingress_keys"
    __table_args__ = (
        Index(
            "uq_public_ingress_public_key",
            "public_key",
            unique=True,
            postgresql_where=text("public_key IS NOT NULL"),
        ),
        Index("ix_public_ingress_org_status", "organisation_id", "status"),
        Index("ix_public_ingress_resource", "resource_type", "resource_id"),
        Index("ix_public_ingress_key_type", "key_type", "status"),
        {"schema": "public"},
    )

    key_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(
        sa_column=Column(
            ForeignKey("public.organisations.organisation_id"),
            nullable=False,
            index=True,
        ),
        description="Tenant owning the target org-schema resource.",
    )
    key_type: PublicIngressKeyType = Field(
        sa_column=Column(String(64), nullable=False),
        description="How this key is intended to be used.",
    )
    resource_type: PublicIngressResourceType = Field(
        sa_column=Column(String(64), nullable=False),
        description="Type of org-schema resource the key resolves to.",
    )
    resource_id: UUID = Field(index=True, description="UUID of the org-schema resource.")
    public_key: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
        description="Browser-safe key or key identifier exposed in public URLs.",
    )
    secret_hash: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Hashed secret for private API keys. Never store raw secrets.",
    )
    status: PublicIngressKeyStatus = Field(
        default=PublicIngressKeyStatus.ACTIVE,
        sa_column=Column(String(32), nullable=False, server_default=text("'active'")),
    )
    allowed_origins: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    allowed_ips: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    rate_limit_policy: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    key_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    expires_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    last_used_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    revoked_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )


__all__ = [
    "PublicIngressKey",
    "PublicIngressKeyStatus",
    "PublicIngressKeyType",
    "PublicIngressResourceType",
]
