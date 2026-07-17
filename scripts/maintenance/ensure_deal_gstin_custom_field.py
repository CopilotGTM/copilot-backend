"""Ensure one organisation has the GSTIN deal custom field used by CSV imports."""

from __future__ import annotations

import argparse
import asyncio
from uuid import UUID

from src.api.schemas.crm_custom_field_models import CreateCustomFieldRequest
from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDataType,
    CRMCustomFieldEntity,
)
from src.infra.database.sessions import org_async_session_scope
from src.service.crm.internal.custom_field_write_service import CustomFieldWriteService
from src.service.crm.internal.internal_crm_setup_service import AUTOMATED_PIPELINE_USER_ID

FIELD_KEY = "gstin_tax_id"
FIELD_LABEL = "GSTIN / Tax ID"


async def ensure_gstin_field(service: CustomFieldWriteService, *, execute: bool) -> str:
    """Return the idempotent action taken for the GSTIN deal custom field."""
    fields = await service.list_fields(
        entity=CRMCustomFieldEntity.DEAL,
        include_inactive=True,
    )
    existing = next((field for field in fields if field.key == FIELD_KEY), None)
    if existing:
        data_type = getattr(existing.data_type, "value", existing.data_type)
        if (
            existing.label != FIELD_LABEL
            or data_type != CRMCustomFieldDataType.TEXT.value
            or not existing.is_active
        ):
            raise RuntimeError(
                f"Existing {FIELD_KEY} field does not match the required active text field"
            )
        return "exists"
    if not execute:
        return "would_create"

    await service.create_field(
        CreateCustomFieldRequest(
            entity=CRMCustomFieldEntity.DEAL,
            key=FIELD_KEY,
            label=FIELD_LABEL,
            data_type=CRMCustomFieldDataType.TEXT,
            required=False,
            description="Validated Indian GSTIN imported from customer migration files.",
        )
    )
    return "created"


async def _run(organisation_id: UUID, *, execute: bool) -> str:
    async with org_async_session_scope(organisation_id) as session:
        service = CustomFieldWriteService(
            session,
            organisation_id,
            AUTOMATED_PIPELINE_USER_ID,
        )
        return await ensure_gstin_field(service, execute=execute)


def main() -> None:
    """Run a dry-run or explicit GSTIN field creation for one organisation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("organisation_id", type=UUID)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Create the field; without this flag the command is read-only.",
    )
    args = parser.parse_args()
    action = asyncio.run(_run(args.organisation_id, execute=args.execute))
    print(f"{args.organisation_id}: {action} {FIELD_KEY} ({FIELD_LABEL})")


if __name__ == "__main__":
    main()
