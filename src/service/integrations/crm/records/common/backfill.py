"""CRMDeal FK-resolution backfill contract.

Defined in phase 2 (pipelines) so phases 3-5 (users, companies, contacts) have
a single, validated API to patch existing deal rows when a referent arrives
late. In the normal dependency-ordered phase run, deals (phase 6) writes with
FKs already resolved — this contract is the safety net for edge cases (e.g. a
company archived at sync time gets reactivated later and needs to patch deals
written while it was invisible).

The contract enforces three op shapes. Anything else raises at construction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import bindparam, text
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
    """Apply FK-resolution UPDATEs to crm_deals.

    Invariants held by every statement this function emits:
      - scope filter: organisation_id = :org AND integration_id = :int
      - is_deleted   = false
      - per-op filter: <external_col> = :external_id
      - idempotency predicate chosen per op shape (see DealFkBackfillOp docstring).

    Caller owns the session's transaction boundary (no commit here) so multiple
    backfill ops from the same sync can land in one write.
    """
    result = BackfillResult()
    if not ops:
        return result

    for op in ops:
        per_op_total = 0
        for external_id, raw_tuple in op.mapping.items():
            internal_id, name, label, email = _unpack_mapping_tuple(raw_tuple)
            sql = _build_update_sql(op)
            params: dict[str, object] = {
                "org": organisation_id,
                "integration": integration_id,
                "external_id": external_id,
                "internal_id": internal_id,
            }
            if op.name_col is not None:
                params["name"] = name
            if op.label_col is not None:
                params["label"] = label
            if op.email_col is not None:
                params["email"] = email

            exec_result = await session.execute(sql, params)
            # rowcount is a CursorResult attribute; typed Result stubs don't expose it.
            per_op_total += getattr(exec_result, "rowcount", 0) or 0

        result.ops_applied += 1
        result.per_op[op.fk_col] = per_op_total
        result.rows_updated += per_op_total

    logger.info(
        "backfill_crm_deals applied",
        extra={
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
            "ops_applied": result.ops_applied,
            "rows_updated": result.rows_updated,
            "per_op": result.per_op,
        },
    )
    return result


def _unpack_mapping_tuple(
    raw: tuple,
) -> tuple[UUID, str | None, str | None, str | None]:
    """Accept either (id, name, label) 3-tuples or (id, name, label, email) 4-tuples.

    The 3-tuple form is the pre-email-column shape kept for caller back-compat.
    Returns a 4-tuple padded with None.
    """
    if len(raw) == 4:
        return raw
    if len(raw) == 3:
        internal_id, name, label = raw
        return internal_id, name, label, None
    raise ValueError(
        f"DealFkBackfillOp mapping value must be a 3- or 4-tuple, got {len(raw)} items"
    )


def _build_update_sql(op: DealFkBackfillOp):
    """Build the parameterised UPDATE statement for a single op.

    Column names come from the op (already validated against an allowlist in
    `__post_init__`), so it is safe to interpolate them into the SQL string.
    All values use bound parameters.
    """
    set_clauses: list[str] = [f"{op.fk_col} = :internal_id"]
    predicate_terms: list[str] = [f"{op.fk_col} IS NULL"]
    if op.name_col is not None:
        set_clauses.append(f"{op.name_col} = :name")
        predicate_terms.append(f"{op.name_col} IS DISTINCT FROM :name")
    if op.label_col is not None:
        set_clauses.append(f"{op.label_col} = :label")
        predicate_terms.append(f"{op.label_col} IS DISTINCT FROM :label")
    if op.email_col is not None:
        set_clauses.append(f"{op.email_col} = :email")
        predicate_terms.append(f"{op.email_col} IS DISTINCT FROM :email")

    set_sql = ", ".join(set_clauses)
    predicate_sql = " OR ".join(predicate_terms)
    # All column names are validated against module-level allowlists in
    # DealFkBackfillOp.__post_init__; only bind parameters carry user data.
    sql_str = (
        f"UPDATE crm_deals SET {set_sql} "  # noqa: S608  # nosec B608 - identifiers allowlist-validated
        f"WHERE organisation_id = :org "
        f"AND integration_id = :integration "
        f"AND is_deleted = false "
        f"AND {op.external_col} = :external_id "
        f"AND ({predicate_sql})"
    )
    sql = text(sql_str)
    # Declare UUID bindparams explicitly so SQLAlchemy can adapt them.
    sql = sql.bindparams(
        bindparam("org"),
        bindparam("integration"),
        bindparam("internal_id"),
    )
    return sql
