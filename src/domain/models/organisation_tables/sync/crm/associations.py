"""
CRM Object Associations Model.

A generic association model to represent relationships between arbitrary CRM objects
(contact ↔ company, deal ↔ contact roles, activity ↔ entities, etc.) across multiple CRM providers.

This table is designed to be provider-agnostic and supports:
- HubSpot labeled associations (/crm/v4/associations)
- Salesforce OpportunityContactRole, AccountContactRelation
- Pipedrive participants and product links
- Zoho Contact Roles and Related Lists
"""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from ...base_mixins import utcnow
from ..base import FlexibleSyncedRecordMixin
from ..integration_models import EMPTY_JSONB_SQL


class CRMObjectType(str, Enum):
    """Types of CRM objects that can participate in associations."""

    DEAL = "deal"
    CONTACT = "contact"
    COMPANY = "company"
    USER = "user"
    ACTIVITY = "activity"
    TASK = "task"
    MEETING = "meeting"
    CALL = "call"
    EMAIL = "email"
    NOTE = "note"
    PRODUCT = "product"
    LINE_ITEM = "line_item"
    TICKET = "ticket"
    LEAD = "lead"
    QUOTE = "quote"
    CUSTOM = "custom"


class CRMAssociationCategory(str, Enum):
    """Category indicating the origin/type of the association."""

    # Provider-defined standard associations
    HUBSPOT_DEFINED = "hubspot_defined"
    SALESFORCE_STANDARD = "salesforce_standard"
    PIPEDRIVE_STANDARD = "pipedrive_standard"
    ZOHO_STANDARD = "zoho_standard"

    # User-defined custom associations in the provider
    USER_DEFINED = "user_defined"

    # Associations created internally by our system
    INTERNAL = "internal"

    # Unknown/other provider
    OTHER = "other"


class CRMObjectAssociation(FlexibleSyncedRecordMixin, table=True):
    """
    Generic association between two CRM objects.

    This table represents relationships like:
    - Deal ↔ Contact (with role like "Decision Maker")
    - Contact ↔ Company (employment relationship)
    - Activity ↔ Deal/Contact/Company (polymorphic activity links)
    - Deal ↔ Line Item (products on deals)

    Uses FlexibleSyncedRecordMixin so associations can be:
    - Synced from a CRM (integration_id set)
    - Created internally without CRM (integration_id NULL)
    """

    __tablename__ = "crm_object_associations"
    __table_args__ = (
        # Uniqueness for synced associations (provider-supplied external_id)
        UniqueConstraint(
            "integration_id",
            "external_id",
            name="uq_crm_object_association_external",
        ),
        # Uniqueness for logical association (prevent duplicates)
        UniqueConstraint(
            "organisation_id",
            "integration_id",
            "from_object_type",
            "from_external_id",
            "to_object_type",
            "to_external_id",
            "association_type_id",
            name="uq_crm_object_association_logical",
        ),
        # Primary query patterns: find associations from/to a specific object
        Index(
            "ix_crm_assoc_from_object",
            "organisation_id",
            "from_object_type",
            "from_external_id",
        ),
        Index(
            "ix_crm_assoc_to_object",
            "organisation_id",
            "to_object_type",
            "to_external_id",
        ),
        # Find all associations for an org by type
        Index(
            "ix_crm_assoc_org_types",
            "organisation_id",
            "from_object_type",
            "to_object_type",
        ),
        # Find associations by local IDs when resolved
        Index("ix_crm_assoc_from_local", "from_local_id"),
        Index("ix_crm_assoc_to_local", "to_local_id"),
        # Find by role (e.g., all "Decision Maker" associations)
        Index("ix_crm_assoc_role", "organisation_id", "role_label"),
    )

    # ==========================================================================
    # Identity
    # ==========================================================================
    association_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Internal UUID for the association record.",
    )
    external_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider-supplied identifier for the association, if any.",
    )

    # ==========================================================================
    # From Object (source of the relationship)
    # ==========================================================================
    from_object_type: CRMObjectType = Field(
        sa_column=Column(String(32), nullable=False),
        description="Type of the source object (deal, contact, company, etc.).",
    )
    from_external_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider ID of the source object. Null for internal-only associations resolved by local IDs.",
    )
    from_local_id: UUID | None = Field(
        default=None,
        index=True,
        description="Internal UUID of the source object when resolved to local tables.",
    )

    # ==========================================================================
    # To Object (target of the relationship)
    # ==========================================================================
    to_object_type: CRMObjectType = Field(
        sa_column=Column(String(32), nullable=False),
        description="Type of the target object (deal, contact, company, etc.).",
    )
    to_external_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider ID of the target object. Null for internal-only associations resolved by local IDs.",
    )
    to_local_id: UUID | None = Field(
        default=None,
        index=True,
        description="Internal UUID of the target object when resolved to local tables.",
    )

    # ==========================================================================
    # Association Metadata
    # ==========================================================================
    association_category: CRMAssociationCategory = Field(
        default=CRMAssociationCategory.OTHER,
        sa_column=Column(String(32), nullable=False, server_default=text("'other'")),
        description="Category indicating origin of the association (provider-defined, user-defined, internal).",
    )
    association_type_id: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True, index=True),
        description="Provider's association type identifier (e.g., HubSpot association type ID, Salesforce relationship name).",
    )
    association_label: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Human-readable label for the association type from the provider.",
    )

    # ==========================================================================
    # Role Information (especially for contact roles on deals)
    # ==========================================================================
    role_label: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
        description="Role label (e.g., 'Decision Maker', 'Economic Buyer', 'Primary Contact').",
    )
    is_primary: bool = Field(
        default=False,
        description="Whether this is the primary association of its type (e.g., primary contact on deal).",
    )

    # ==========================================================================
    # Flexible Properties
    # ==========================================================================
    properties: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(EMPTY_JSONB_SQL),
        ),
        description="Additional properties specific to this association (e.g., Salesforce ContactRole fields).",
    )

    # ==========================================================================
    # Sync Timestamps
    # ==========================================================================
    source_created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Creation timestamp from the provider.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Last modified timestamp from the provider.",
    )
    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Timestamp when this association was last synced.",
    )
    last_seen_at: datetime | None = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent time this association was observed during sync.",
    )

    # ==========================================================================
    # Raw Payload
    # ==========================================================================
    data_payload: dict | None = Field(
        default=None,
        sa_column=Column(JSONB),
        description="Raw provider payload for debugging and replay.",
    )


__all__ = [
    "CRMObjectAssociation",
    "CRMObjectType",
    "CRMAssociationCategory",
]
