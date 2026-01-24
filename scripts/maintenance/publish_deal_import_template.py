from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from src.infra.storage.azure_app_blob_storage import AzureApplicationBlobStorage

# Keep in sync with DealImportService defaults without importing service modules.
TEMPLATE_DIR = "crm_templates"
TEMPLATE_NAME = "deal_import_template"


def build_csv_template() -> bytes:
    # Keep headers aligned with DealImportService._build_csv_template
    headers = [
        "deal_name",
        "owner_email",
        "pipeline_name",
        "stage_name",
        "amount",
        "currency",
        "close_date",
        "expected_close_date",
        "deal_type",
        "priority",
        "lead_source",
        "description",
        "company_domain",
        "company_name",
        "company_website",
        "company_linkedin",
        "team_member_emails",
    ]
    content = ",".join(headers) + "\n"
    return content.encode("utf-8")


async def publish_template(*, output_path: Path | None, overwrite: bool) -> None:
    blob_path = f"{TEMPLATE_DIR}/{TEMPLATE_NAME}.csv"
    storage = AzureApplicationBlobStorage()
    try:
        template_bytes = build_csv_template()

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
        "--overwrite",
        action="store_true",
        help="Overwrite existing template in storage.",
    )
    args = parser.parse_args()
    asyncio.run(publish_template(output_path=args.output, overwrite=args.overwrite))


if __name__ == "__main__":
    main()
