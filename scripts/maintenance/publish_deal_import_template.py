from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from src.infra.storage.azure_app_blob_storage import AzureApplicationBlobStorage

# Keep in sync with DealImportService defaults without importing service modules.
TEMPLATE_DIR = "crm_templates"
TEMPLATE_NAME = "deal_import_template"
# App-blob storage path where this template is saved.
TEMPLATE_BLOB_PATH = f"{TEMPLATE_DIR}/{TEMPLATE_NAME}.csv"


def build_csv_template() -> bytes:
    # Keep headers aligned with DealImportService._build_csv_template
    headers = [
        "deal_name",
        "deal_amount",
        "deal_currency",
        "deal_status",
        "deal_probability",
        "deal_close_date",
        "deal_expected_close_date",
        "deal_closed_won_at",
        "deal_closed_lost_at",
        "deal_closed_reason",
        "deal_stage_name",
        "deal_forecast_category",
        "deal_type",
        "deal_priority",
        "deal_lead_source",
        "deal_owner_email",
        "company_name",
        "company_domain",
        "deal_primary_contact_name",
        "deal_primary_contact_email",
        "deal_source_created_at",
        "deal_last_activity_at",
        "deal_description",
        # Related account/team fields (not on crm_deals directly)
        "company_website",
        "company_linkedin",
        "company_industry",
        "company_country",
        "deal_team_member_emails",
    ]
    content = ",".join(headers) + "\n"
    return content.encode("utf-8")


async def publish_template(*, output_path: Path | None, overwrite: bool) -> None:
    blob_path = TEMPLATE_BLOB_PATH
    storage = AzureApplicationBlobStorage()
    try:
        template_bytes = build_csv_template()
        print(f"Template storage path: {blob_path}")

        if output_path:
            output_path.write_bytes(template_bytes)

        if not overwrite:
            existing = await storage.get_blob_properties(blob_path)
            if existing is not None:
                print(f"Template already exists in storage: {blob_path}")
                return

        await storage.upload_bytes(blob_path, template_bytes, content_type="text/csv")
        print(f"Uploaded template to storage: {blob_path}")
    finally:
        await storage.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish deal import CSV template to app storage.")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional local path to write the CSV template.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Do not overwrite if template already exists in storage.",
    )
    args = parser.parse_args()
    asyncio.run(publish_template(output_path=args.output, overwrite=not args.skip_existing))


if __name__ == "__main__":
    main()
