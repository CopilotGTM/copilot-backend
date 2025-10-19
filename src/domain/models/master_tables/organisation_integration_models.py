from uuid import UUID

from sqlalchemy import Column, String
from sqlmodel import Field

from .base_mixins import TimestampMixin


class OrganisationIntegrationDetails(TimestampMixin, table=True):
    __tablename__ = "organisation_integration_details"

    connection_id: UUID = Field(primary_key=True, description="Nango connection identifier")
    organisation_id: UUID = Field(index=True, description="Organisation owning the connection")
    integration_id: UUID = Field(index=True, description="Tenant integration ID")
    provider_config_key: str = Field(
        sa_column=Column(String(255), nullable=False, index=True),
        description="Nango provider config key",
    )
