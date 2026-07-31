from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import Column, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from ...base_mixins import TimestampMixin
from ..integration_models import EMPTY_JSONB_SQL
from .base import tenant_foreign_key


class CRMImportRowStatus(StrEnum):
    PREVIEW = "preview"
    READY = "ready"
    CREATED = "created"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    FAILED = "failed"


class CRMImportRow(TimestampMixin, table=True):
    """Row-level ledger for a CRM CSV import stored under a DealJobs parent."""

    __tablename__ = "crm_import_rows"
    __table_args__ = (
        tenant_foreign_key(
            "crm_import_rows",
            "import_job_id",
            "deal_jobs",
            "job_id",
            ondelete="CASCADE",
        ),
        Index(
            "ux_crm_import_rows_import_row_number",
            "organisation_id",
            "import_job_id",
            "row_number",
            unique=True,
        ),
        Index(
            "ix_crm_import_rows_import_status",
            "organisation_id",
            "import_job_id",
            "status",
        ),
        Index("ix_crm_import_rows_org_status", "organisation_id", "status"),
    )

    row_id: UUID = Field(default_factory=uuid4, primary_key=True)
    import_job_id: UUID = Field(
        sa_column=Column(
            sa.Uuid,
            nullable=False,
            index=True,
        ),
        description="Parent DealJobs.job_id for this import.",
    )
    organisation_id: UUID = Field(index=True)
    row_number: int = Field(description="One-based CSV data row number.")
    raw_values: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    normalized_values: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
    )
    status: CRMImportRowStatus = Field(
        default=CRMImportRowStatus.PREVIEW,
        sa_column=Column(String(32), nullable=False, index=True),
    )
    target_type: str | None = Field(default=None, sa_column=Column(String(32), nullable=True))
    account_id: UUID | None = Field(default=None, index=True)
    contact_id: UUID | None = Field(default=None, index=True)
    deal_id: UUID | None = Field(default=None, index=True)
    error_code: str | None = Field(default=None, sa_column=Column(String(64), nullable=True))
    error_message: str | None = None
