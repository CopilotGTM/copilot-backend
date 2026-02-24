from __future__ import annotations

import csv
import io
import re
from datetime import UTC
from typing import Any
from uuid import UUID, uuid4

from src.infra.storage.azure_app_blob_storage import AzureApplicationBlobStorage
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage


class DealImportServiceError(Exception):
    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class DealImportService:
    TEMPLATE_DIR = "crm_templates"
    IMPORT_DIR = "crm_imports/deals"
    TEMPLATE_NAME = "deal_import_template"
    TEMPLATE_HEADERS = [
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
    UPLOAD_EXPIRY_MINUTES = 15
    DOWNLOAD_EXPIRY_MINUTES = 60

    _SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
    _CUSTOM_PREFIX_RE = re.compile(r"^custom_(.+)$")
    _CUSTOM_KEY_SANITIZE_RE = re.compile(r"[^a-z0-9_]+")
    _CUSTOM_KEY_ALIASES = {
        # Backward compatibility for client-specific historical sheets.
        "deepmatrix_revenue_inr": "our_revenue_inr",
    }
    _TRUE_VALUES = {"true", "yes", "y"}
    _FALSE_VALUES = {"false", "no", "n"}
    _CURRENCY_SYMBOL_RE = re.compile(r"[$€£¥₹]")
    _CURRENCY_CODE_RE = re.compile(r"(?<![A-Za-z])[A-Z]{3}(?![A-Za-z])")
    _TIME_TOKEN_RE = re.compile(r"\d{1,2}:\d{2}")

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

        # Always publish the latest template to avoid serving stale headers after updates.
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
        # UUID in path prevents collisions when the same user uploads multiple files
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

    async def inspect_stage_names(self, storage_path: str) -> list[str]:
        """Extract distinct stage names from the CSV, preserving first-seen order."""
        content = await self.org_storage.get_blob_content(storage_path)
        text = content.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return []

        # Find the stage column via known aliases
        stage_aliases = {"deal_stage_name", "stage_name", "stage"}
        stage_header: str | None = None
        for field in reader.fieldnames:
            if field and field.strip().lower() in stage_aliases:
                stage_header = field
                break
        if not stage_header:
            return []

        seen: set[str] = set()
        ordered: list[str] = []
        for row in reader:
            value = self._clean_value(row.get(stage_header))
            if value and value.lower() not in seen:
                seen.add(value.lower())
                ordered.append(value)
        return ordered

    async def inspect_custom_columns(self, storage_path: str) -> tuple[list[str], dict[str, str]]:
        """Inspect uploaded CSV and return custom column keys with suggested scalar types."""
        content = await self.org_storage.get_blob_content(storage_path)
        text = content.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise DealImportServiceError("Import file is missing a header row", status_code=400)

        custom_columns_by_header: dict[str, str] = {}
        custom_keys_in_order: list[str] = []
        for raw_header in reader.fieldnames:
            if raw_header is None:
                continue
            header = raw_header.strip().lower()
            match = self._CUSTOM_PREFIX_RE.match(header)
            if not match:
                continue
            custom_key = self._normalize_custom_key(match.group(1))
            if custom_key not in custom_keys_in_order:
                custom_keys_in_order.append(custom_key)
            custom_columns_by_header[header] = custom_key

        if not custom_columns_by_header:
            return [], {}

        samples_by_key: dict[str, list[str]] = {key: [] for key in custom_keys_in_order}
        for row_number, row in enumerate(reader, start=2):
            extras = row.get(None)
            if extras:
                raise DealImportServiceError(
                    f"Invalid CSV format on row {row_number}: too many columns",
                    status_code=400,
                )
            for raw_header, raw_value in row.items():
                if raw_header is None:
                    continue
                header = raw_header.strip().lower()
                custom_key = custom_columns_by_header.get(header)
                if not custom_key:
                    continue
                value = self._clean_value(raw_value)
                if value is None:
                    continue
                samples = samples_by_key[custom_key]
                if len(samples) < 100:
                    samples.append(value)

        suggestions = {
            key: self._infer_scalar_type(samples_by_key.get(key, []))
            for key in custom_keys_in_order
        }
        return custom_keys_in_order, suggestions

    @staticmethod
    def humanize_custom_key(key: str) -> str:
        """Convert machine key to a readable label."""
        words = [part for part in key.replace("_", " ").split() if part]
        if not words:
            return key
        return " ".join(word.capitalize() for word in words)

    @classmethod
    def _normalize_custom_key(cls, raw_key: str) -> str:
        key = raw_key.strip().lower()
        if key.startswith("custom_"):
            key = key[len("custom_") :]
        key = key.replace("-", "_").replace(" ", "_")
        key = cls._CUSTOM_KEY_SANITIZE_RE.sub("_", key)
        key = re.sub(r"_+", "_", key).strip("_")
        key = cls._CUSTOM_KEY_ALIASES.get(key, key)
        if not key:
            raise DealImportServiceError("Invalid custom column name in header", status_code=400)
        if not re.match(r"^[a-z][a-z0-9_]{1,62}$", key):
            raise DealImportServiceError(
                f"Invalid custom column key: {raw_key}",
                status_code=400,
            )
        return key

    @staticmethod
    def _clean_value(value: Any) -> str | None:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    @classmethod
    def _infer_scalar_type(cls, samples: list[str]) -> str:
        if not samples:
            return "text"
        if cls._all_integer(samples):
            return "integer"
        if cls._all_boolean(samples):
            return "boolean"
        if cls._all_datetime(samples):
            return "datetime"
        if cls._all_date(samples):
            return "date"
        if cls._all_currency(samples):
            return "currency"
        if cls._all_percentage(samples):
            return "percentage"
        if cls._all_number(samples):
            return "number"
        return "text"

    @staticmethod
    def _all_integer(values: list[str]) -> bool:
        for value in values:
            try:
                int(value)
            except ValueError:
                return False
        return True

    @staticmethod
    def _all_number(values: list[str]) -> bool:
        for value in values:
            try:
                float(value)
            except ValueError:
                return False
        return True

    @classmethod
    def _all_boolean(cls, values: list[str]) -> bool:
        for value in values:
            lowered = value.strip().lower()
            if lowered not in cls._TRUE_VALUES and lowered not in cls._FALSE_VALUES:
                return False
        return True

    @staticmethod
    def _all_date(values: list[str]) -> bool:
        for value in values:
            try:
                from dateutil import parser as date_parser  # type: ignore[import-untyped]

                parsed = date_parser.parse(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                _ = parsed.isoformat()
            except Exception:
                return False
        return True

    @classmethod
    def _all_datetime(cls, values: list[str]) -> bool:
        for value in values:
            # Keep datetime distinct from date by requiring explicit time in the source value.
            lowered = value.strip().lower()
            if "t" not in lowered and not cls._TIME_TOKEN_RE.search(lowered):
                return False
            try:
                from dateutil import parser as date_parser  # type: ignore[import-untyped]

                parsed = date_parser.parse(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                _ = parsed.isoformat()
            except Exception:
                return False
        return True

    @classmethod
    def _all_currency(cls, values: list[str]) -> bool:
        saw_currency_marker = False
        for value in values:
            if cls._CURRENCY_SYMBOL_RE.search(value) or cls._CURRENCY_CODE_RE.search(
                value.strip().upper()
            ):
                saw_currency_marker = True

            normalized = value.strip()
            normalized = cls._CURRENCY_SYMBOL_RE.sub("", normalized)
            normalized = cls._CURRENCY_CODE_RE.sub("", normalized)
            normalized = normalized.replace(",", "").replace(" ", "")
            if not normalized:
                return False
            try:
                float(normalized)
            except ValueError:
                return False
        # Avoid classifying plain numeric columns as currency when no marker is present.
        return saw_currency_marker

    @staticmethod
    def _all_percentage(values: list[str]) -> bool:
        """Detect percentage columns: values with '%' suffix or all numbers in 0–1 range."""
        saw_pct_sign = False
        for value in values:
            stripped = value.strip()
            if stripped.endswith("%"):
                saw_pct_sign = True
                stripped = stripped[:-1].strip()
            try:
                num = float(stripped)
            except ValueError:
                return False
            # Without an explicit % sign, only treat 0–1 range as percentage.
            if not saw_pct_sign and not (0 <= num <= 1):
                return False
        return True
