"""Master-schema indexes that locate tenant-owned integrations."""

from uuid import UUID

from sqlalchemy import Column, String
from sqlmodel import Field

from .base_mixins import TimestampMixin


class OrganisationIntegrationDetails(TimestampMixin, table=True):
    """Locate a tenant integration without making master data authoritative."""

    __tablename__ = "organisation_integration_details"

    connection_id: UUID = Field(primary_key=True, description="Provider connection identifier")
    organisation_id: UUID = Field(index=True, description="Organisation owning the connection")
    integration_id: UUID = Field(index=True, description="Tenant integration ID")
    provider_config_key: str = Field(
        sa_column=Column(String(255), nullable=False, index=True),
        description="Provider configuration key",
    )
    integration_email: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True, index=True),
        description="Normalized connected-account email used for webhook routing",
    )
