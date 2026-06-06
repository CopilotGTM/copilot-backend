"""Compatibility shim for the old CRMDeal external-ID FK backfill contract.

Step 5 removes the provider external-ID columns from `crm_deals`. Writers now
resolve local IDs before persistence, so the old UPDATE-by-external-column
backfill path intentionally becomes a no-op until its callers are removed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)

# Columns the contract is allowed to mention on crm_deals. Any caller passing a
# column outside this set is a bug — the post-init validator rejects early
# rather than producing a silent UPDATE against the wrong table.
_ALLOWED_EXTERNAL_COLS: frozenset[str] = frozenset(
    {
        "pipeline_external_id",
        "stage_external_id",
        "owner_external_id",
        "company_external_id",
        "primary_contact_external_id",
        "created_by_user_external_id",
        "updated_by_user_external_id",
    }
)
_ALLOWED_FK_COLS: frozenset[str] = frozenset(
    {
        "pipeline_id",
        "stage_id",
        "owner_id",
        "company_id",
        "primary_contact_id",
        "created_by_user_id",
        "updated_by_user_id",
    }
)
_ALLOWED_DISPLAY_COLS: frozenset[str] = frozenset(
    {
        "pipeline_name",
        "pipeline_label",
        "stage_name",
        "stage_label",
        "owner_name",
        "owner_email",
        "company_name",
        "company_domain",
        "primary_contact_name",
        "primary_contact_email",
    }
)


@dataclass(slots=True, frozen=True)
class DealFkBackfillOp:
    """One batched UPDATE op describing how to resolve a CRMDeal FK column.

    Valid shapes:
      1. FK-only:             name_col=None, label_col=None, email_col=None
      2. FK + name:           name_col=<col>, label_col=None, email_col=None
      3. FK + name + label:   name_col=<col>, label_col=<col>, email_col=None
      4. FK + name + email:   name_col=<col>, label_col=None, email_col=<col>

    `name_col=None` with either `label_col` or `email_col` set is rejected —
    there is no well-defined semantics for "update label/email without name,"
    and silent drift would follow.

    `mapping` values are 4-tuples regardless of shape:
        (internal_id, name_or_None, label_or_None, email_or_None)
    For backwards-compatibility with callers that predate email_col, the
    validator also accepts 3-tuples (internal_id, name, label) by padding.
    """

    external_col: str
    fk_col: str
    mapping: Mapping[str, tuple]
    name_col: str | None = None
    label_col: str | None = None
    email_col: str | None = None

    def __post_init__(self) -> None:
        if self.external_col not in _ALLOWED_EXTERNAL_COLS:
            raise ValueError(f"external_col {self.external_col!r} is not an allowed CRMDeal column")
        if self.fk_col not in _ALLOWED_FK_COLS:
            raise ValueError(f"fk_col {self.fk_col!r} is not an allowed CRMDeal FK column")
        if self.name_col is not None and self.name_col not in _ALLOWED_DISPLAY_COLS:
            raise ValueError(f"name_col {self.name_col!r} is not an allowed display column")
        if self.label_col is not None and self.label_col not in _ALLOWED_DISPLAY_COLS:
            raise ValueError(f"label_col {self.label_col!r} is not an allowed display column")
        if self.email_col is not None and self.email_col not in _ALLOWED_DISPLAY_COLS:
            raise ValueError(f"email_col {self.email_col!r} is not an allowed display column")
        if self.name_col is None and (self.label_col is not None or self.email_col is not None):
            raise ValueError(
                "label_col or email_col is set but name_col is None — this shape is not supported"
            )
        if not self.mapping:
            raise ValueError("mapping cannot be empty")


@dataclass(slots=True)
class BackfillResult:
    """Counters returned from `backfill_crm_deals`."""

    rows_updated: int = 0
    ops_applied: int = 0
    # Per-op update counts for observability. Keyed by fk_col so callers can tell
    # which backfill slice actually touched anything on a given run.
    per_op: dict[str, int] = field(default_factory=dict)


async def backfill_crm_deals(
    session: AsyncSession,
    *,
    organisation_id: UUID,
    integration_id: UUID,
    ops: Sequence[DealFkBackfillOp],
) -> BackfillResult:
    """Return a no-op result for legacy callers.

    The provider IDs needed for this old path now live in
    `crm_source_object_mappings`, not on `crm_deals`.
    """
    del session
    result = BackfillResult()
    if not ops:
        return result

    for op in ops:
        result.ops_applied += 1
        result.per_op[op.fk_col] = 0

    logger.info(
        "backfill_crm_deals skipped after CRM external-id column removal",
        extra={
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
            "ops_applied": result.ops_applied,
            "rows_updated": result.rows_updated,
            "per_op": result.per_op,
        },
    )
    return result
