"""Service for creating CRM leads from private lead-ingestion requests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.schemas.private_lead_ingestion_models import (
    MAX_PRIVATE_PAYLOAD_BYTES,
    MAX_PRIVATE_PAYLOAD_DEPTH,
    MAX_PRIVATE_PAYLOAD_LIST_ITEMS,
    MAX_PRIVATE_PAYLOAD_NODES,
    MAX_PRIVATE_SCALAR_STRING_LENGTH,
    PrivateLeadIngestionRequest,
)
from src.domain.models.organisation_tables.sync.crm.leads import (
    CRMLead,
    CRMLeadSourceType,
)


class PrivateLeadIngestionServiceError(Exception):
    """Private lead ingestion validation or persistence error."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        field: str | None = None,
    ):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.field = field
        super().__init__(message)


class PrivateLeadIngestionService:
    """Create CRM leads from normalized private ingestion payloads."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id

    async def ingest(
        self,
        *,
        payload: PrivateLeadIngestionRequest,
        key_id: str,
        idempotency_key: str | None,
    ) -> tuple[CRMLead, bool]:
        """Ingest a payload with idempotency support and return created/replayed lead state."""
        if not idempotency_key or not idempotency_key.strip():
            raise PrivateLeadIngestionServiceError(
                "missing_idempotency_key",
                "Idempotency-Key header is required",
                400,
            )
        idempotency_key = idempotency_key.strip()
        self._validate_payload(payload)
        existing = await self._find_existing(key_id=key_id, idempotency_key=idempotency_key)
        if existing:
            return existing, True

        now = datetime.now(UTC)
        lead_payload = payload.model_dump(exclude_unset=True)
        source_payload = lead_payload.pop("data_payload", {}) or {}
        lead_payload["email"] = self._normalize_email(lead_payload.get("email"))
        lead_payload["owner_email"] = self._normalize_email(lead_payload.get("owner_email"))
        lead_payload["company_domain"] = self._normalize_domain(lead_payload.get("company_domain"))
        lead_payload["source_type"] = lead_payload.get("source_type") or CRMLeadSourceType.API
        lead_payload["captured_at"] = now
        lead_payload["data_payload"] = {
            "ingestion": {
                "key_id": key_id,
                "idempotency_key": idempotency_key,
                "received_at": now.isoformat(),
            },
            "source_payload": source_payload,
        }
        lead = CRMLead(
            **lead_payload,
            organisation_id=self.organisation_id,
            ingestion_key_id=key_id,
            idempotency_key=idempotency_key,
        )
        self.org_session.add(lead)
        # Commit can race under concurrent retries with the same private key. Roll
        # back and requery so a uniqueness conflict returns the winner row instead
        # of failing the request.
        try:
            await self.org_session.commit()
        except IntegrityError:
            await self.org_session.rollback()
            existing = await self._find_existing(key_id=key_id, idempotency_key=idempotency_key)
            if existing:
                return existing, True
            raise
        await self.org_session.refresh(lead)
        return lead, False

    async def _find_existing(self, *, key_id: str, idempotency_key: str) -> CRMLead | None:
        """Find an existing lead by ingestion key and idempotency token."""
        # Use dedicated replay columns for an exact indexed lookup. The org id + key
        # + idempotency tuple is the tenancy boundary and replay contract.
        stmt = select(CRMLead).where(
            col(CRMLead.organisation_id) == self.organisation_id,
            col(CRMLead.source_type) == CRMLeadSourceType.API.value,
            col(CRMLead.form_submission_id).is_(None),
            col(CRMLead.ingestion_key_id) == key_id,
            col(CRMLead.idempotency_key) == idempotency_key,
        )
        return (await self.org_session.execute(stmt)).scalars().first()

    def _validate_payload(self, payload: PrivateLeadIngestionRequest) -> None:
        """Validate payload identity fields, email syntax hints, and source-type constraints."""
        if payload.source_type == CRMLeadSourceType.FORM:
            raise PrivateLeadIngestionServiceError(
                "invalid_source_type",
                "Private ingestion cannot create form-sourced leads",
                422,
                "source_type",
            )
        status_value = payload.status.value if hasattr(payload.status, "value") else payload.status
        if status_value == "converted":
            raise PrivateLeadIngestionServiceError(
                "converted_status_not_allowed",
                "Use the lead conversion endpoint to set converted status",
                422,
                "status",
            )
        if not any([payload.email, payload.phone, payload.full_name, payload.company_name]):
            raise PrivateLeadIngestionServiceError(
                "missing_lead_identity",
                "Lead must include at least one of email, phone, full_name, or company_name",
                422,
            )
        if payload.email and "@" not in payload.email:
            raise PrivateLeadIngestionServiceError("invalid_email", "Invalid email", 422, "email")
        if payload.owner_email and "@" not in payload.owner_email:
            raise PrivateLeadIngestionServiceError(
                "invalid_email",
                "Invalid owner email",
                422,
                "owner_email",
            )
        self._validate_payload_budget(payload.model_dump(mode="json", exclude_none=True))

    def _validate_payload_budget(self, payload: dict[str, Any]) -> None:
        """Apply size and node-count constraints to prevent oversized payload abuse."""
        encoded_size = len(str(payload).encode("utf-8"))
        if encoded_size > MAX_PRIVATE_PAYLOAD_BYTES:
            raise PrivateLeadIngestionServiceError("payload_too_large", "Payload is too large", 413)
        node_count = self._validate_value(payload, depth=0, field="body")
        if node_count > MAX_PRIVATE_PAYLOAD_NODES:
            raise PrivateLeadIngestionServiceError(
                "payload_too_complex",
                "Payload has too many fields",
                413,
            )

    def _validate_value(self, value: Any, *, depth: int, field: str) -> int:
        """Recursively validate nested payload shape, size, and traversal depth."""
        if depth > MAX_PRIVATE_PAYLOAD_DEPTH:
            raise PrivateLeadIngestionServiceError(
                "payload_too_deep",
                "Payload nesting is too deep",
                413,
                field,
            )
        if isinstance(value, str) and len(value) > MAX_PRIVATE_SCALAR_STRING_LENGTH:
            raise PrivateLeadIngestionServiceError(
                "payload_too_large",
                "Payload field value is too large",
                413,
                field,
            )
        if isinstance(value, dict):
            count = 1
            for key, child in value.items():
                count += self._validate_value(child, depth=depth + 1, field=f"{field}.{key}")
            return count
        if isinstance(value, list):
            if len(value) > MAX_PRIVATE_PAYLOAD_LIST_ITEMS:
                raise PrivateLeadIngestionServiceError(
                    "payload_too_large",
                    "Payload list is too large",
                    413,
                    field,
                )
            count = 1
            for index, child in enumerate(value):
                count += self._validate_value(child, depth=depth + 1, field=f"{field}[{index}]")
            return count
        return 1

    @staticmethod
    def _normalize_email(value: Any) -> str | None:
        """Normalize optional email values to lowercase trimmed value for matching consistency."""
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower()
        return normalized or None

    @staticmethod
    def _normalize_domain(value: Any) -> str | None:
        """Normalize company domain values by removing scheme/path and trimming whitespace."""
        if not isinstance(value, str):
            return None
        domain = value.strip().lower()
        domain = domain.removeprefix("https://").removeprefix("http://")
        domain = domain.split("/", maxsplit=1)[0]
        return domain or None
