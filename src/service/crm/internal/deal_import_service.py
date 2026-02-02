from __future__ import annotations

import re
from uuid import UUID, uuid4

from src.infra.storage.azure_app_blob_storage import AzureApplicationBlobStorage
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage


class DealImportServiceError(Exception):
    # Custom exception for deal import errors.

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class DealImportService:
    # Handles template download and upload URL generation for deal imports.

    TEMPLATE_DIR = "crm_templates"
    IMPORT_DIR = "crm_imports/deals"
    TEMPLATE_NAME = "deal_import_template"
    TEMPLATE_HEADERS = [
        "deal_name",
        "owner_email",
        "pipeline_name",
        "stage_name",
        "amount",
        "currency",
        "deal_status",
        "probability",
        "forecast_category",
        "close_date",
        "expected_close_date",
        "source_created_at",
        "last_activity_at",
        "closed_won_at",
        "closed_lost_at",
        "closed_reason",
        "deal_type",
        "priority",
        "lead_source",
        "crm_url",
        "description",
        "company_domain",
        "company_name",
        "company_website",
        "company_linkedin",
        "primary_contact_name",
        "primary_contact_email",
        "team_member_emails",
    ]
    UPLOAD_EXPIRY_MINUTES = 15
    DOWNLOAD_EXPIRY_MINUTES = 60

    _SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")

    def __init__(self, organisation_id: UUID, user_id: UUID):
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.org_storage = AzureOrganisationBlobStorage(organisation_id)
        self.app_storage = AzureApplicationBlobStorage()

    async def close(self) -> None:
        await self.org_storage.close()
        await self.app_storage.close()

    async def get_template_download(self, file_type: str) -> tuple[str, str, int]:
        if file_type != "csv":
            raise DealImportServiceError("Only CSV templates are supported", status_code=400)

        blob_path = f"{self.TEMPLATE_DIR}/{self.TEMPLATE_NAME}.{file_type}"
        props = await self.app_storage.get_blob_properties(blob_path)
        if props is None:
            template_bytes = self._build_csv_template()
            await self.app_storage.upload_bytes(blob_path, template_bytes, content_type="text/csv")

        download_url = await self.app_storage.generate_read_sas(
            blob_path,
            minutes=self.DOWNLOAD_EXPIRY_MINUTES,
            download_filename=f"{self.TEMPLATE_NAME}.{file_type}",
        )
        return download_url, blob_path, self.DOWNLOAD_EXPIRY_MINUTES * 60

    async def get_upload_url(self, filename: str) -> tuple[str, str, int]:
        safe_name = self._sanitize_filename(filename)
        upload_id = uuid4()
        blob_path = f"{self.IMPORT_DIR}/{self.user_id}/{upload_id}/{safe_name}"
        upload_url = await self.org_storage.generate_write_sas(
            blob_path, minutes=self.UPLOAD_EXPIRY_MINUTES
        )
        return upload_url, blob_path, self.UPLOAD_EXPIRY_MINUTES * 60

    def _sanitize_filename(self, name: str) -> str:
        base = name.strip().replace(" ", "_")
        base = self._SAFE_FILENAME.sub("_", base)
        return base or "file"

    def _build_csv_template(self) -> bytes:
        content = ",".join(self.TEMPLATE_HEADERS) + "\n"
        return content.encode("utf-8")
