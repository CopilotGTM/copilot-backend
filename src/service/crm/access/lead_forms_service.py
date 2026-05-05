"""Services for lead form configuration and public lead capture."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.schemas.lead_form_models import (
    MAX_PUBLIC_FIELD_KEY_LENGTH,
    MAX_PUBLIC_SCALAR_STRING_LENGTH,
    CreateLeadFormRequest,
    PublicLeadFormDefinitionResponse,
    PublicLeadFormFieldItem,
    UpdateLeadFormRequest,
)
from src.domain.models.master_tables import (
    PublicIngressKey,
    PublicIngressKeyStatus,
    PublicIngressKeyType,
    PublicIngressResourceType,
)
from src.domain.models.organisation_tables.sync.crm.lead_forms import (
    LeadForm,
    LeadFormField,
    LeadFormFieldDataType,
    LeadFormFieldTargetType,
    LeadFormStatus,
    LeadFormSubmission,
)
from src.domain.models.organisation_tables.sync.crm.leads import (
    CRMLead,
    CRMLeadSourceType,
    CRMLeadStatus,
)
from src.infra.utils.logger import get_logger
from src.service.crm.internal.internal_crm_user_service import (
    InternalCRMUserService,
    InternalCRMUserServiceError,
)

logger = get_logger("lead_forms_service")

_PUBLIC_KEY_PREFIX = "lf_"
_LEAD_FIELD_TARGETS = {
    "first_name",
    "last_name",
    "full_name",
    "email",
    "phone",
    "job_title",
    "linkedin_url",
    "company_name",
    "company_domain",
    "company_website",
    "industry",
    "country",
    "region",
    "city",
    "message",
    "interest",
    "budget_amount",
    "budget_currency",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
}


class LeadFormsServiceError(Exception):
    """Public and authenticated lead form service error."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)


class LeadFormsService:
    """Authenticated CRUD for lead form definitions."""

    def __init__(
        self,
        org_session: AsyncSession,
        master_session: AsyncSession,
        organisation_id: UUID,
        actor_user_id: UUID | None,
    ):
        self.org_session = org_session
        self.master_session = master_session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id

    async def close(self) -> None:
        """Close org/master async sessions used by form write/read workflows."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("LeadFormsService org close failed", exc_info=True)
        try:
            await self.master_session.close()
        except Exception:
            logger.debug("LeadFormsService master close failed", exc_info=True)

    async def list_forms(
        self,
    ) -> list[tuple[LeadForm, list[LeadFormField], PublicIngressKey | None]]:
        """Return all non-deleted forms with fields and linked public key for each form."""
        stmt = (
            select(LeadForm)
            .where(
                col(LeadForm.organisation_id) == self.organisation_id,
                col(LeadForm.is_deleted).is_(False),
            )
            .order_by(col(LeadForm.created_at).desc())
        )
        forms = list((await self.org_session.execute(stmt)).scalars().all())
        if not forms:
            return []
        fields_by_form = await self._load_fields_by_form([form.form_id for form in forms])
        keys_by_form = await self._load_public_form_keys([form.form_id for form in forms])
        return [
            (form, fields_by_form.get(form.form_id, []), keys_by_form.get(form.form_id))
            for form in forms
        ]

    async def get_form(
        self, form_id: UUID
    ) -> tuple[LeadForm, list[LeadFormField], PublicIngressKey | None]:
        """Resolve one form plus its current fields and public key linkage."""
        form = await self._get_form_or_404(form_id)
        fields = await self._load_fields(form.form_id)
        key = (await self._load_public_form_keys([form.form_id])).get(form.form_id)
        return form, fields, key

    async def update_form_status(
        self,
        form_id: UUID,
        status: LeadFormStatus,
    ) -> tuple[LeadForm, list[LeadFormField], PublicIngressKey | None]:
        """Update status and sync linked public ingestion key state with transaction safety."""
        try:
            form = await self._get_form_or_404(form_id)
            actor_crm_user_id = await self._resolve_actor_crm_user_id()
            ingress_key = (await self._load_public_form_keys([form.form_id])).get(form.form_id)

            form.status = status
            form.updated_by_user_id = actor_crm_user_id
            self.org_session.add(form)
            await self.org_session.flush()

            if ingress_key:
                ingress_key.status = (
                    PublicIngressKeyStatus.ACTIVE
                    if status == LeadFormStatus.ACTIVE
                    else PublicIngressKeyStatus.PAUSED
                )
                self.master_session.add(ingress_key)
                await self.master_session.flush()

            await self.org_session.commit()
            await self.master_session.commit()
            await self.org_session.refresh(form)
            if ingress_key:
                await self.master_session.refresh(ingress_key)
            fields = await self._load_fields(form.form_id)
        except Exception:
            await self.org_session.rollback()
            await self.master_session.rollback()
            raise
        return form, fields, ingress_key

    async def update_form(
        self,
        form_id: UUID,
        request: UpdateLeadFormRequest,
    ) -> tuple[LeadForm, list[LeadFormField], PublicIngressKey | None]:
        """Update metadata and fields while preserving versioning and audit attribution."""
        self._validate_field_requests(request.fields)
        try:
            form = await self._get_form_or_404(form_id)
            actor_crm_user_id = await self._resolve_actor_crm_user_id()
            existing_fields = await self._load_fields(form.form_id)
            ingress_key = (await self._load_public_form_keys([form.form_id])).get(form.form_id)
            next_version = form.current_version + 1

            form.name = request.name.strip()
            form.description = request.description
            form.status = request.status
            form.current_version = next_version
            form.settings = request.settings
            form.updated_by_user_id = actor_crm_user_id
            self.org_session.add(form)

            existing_by_key = {field.external_key: field for field in existing_fields}
            requested_keys: set[str] = set()
            updated_fields: list[LeadFormField] = []
            for index, field_request in enumerate(request.fields):
                field = existing_by_key.get(field_request.external_key.strip())
                if field is None:
                    field = LeadFormField(
                        organisation_id=self.organisation_id,
                        form_id=form.form_id,
                        external_key=field_request.external_key.strip(),
                        label=field_request.label.strip(),
                    )
                self._apply_field_request(field, field_request, index, next_version)
                requested_keys.add(field.external_key)
                updated_fields.append(field)
                self.org_session.add(field)

            for field in existing_fields:
                if field.external_key not in requested_keys:
                    self._archive_removed_field(field)
                    self.org_session.add(field)

            await self.org_session.flush()

            if ingress_key:
                ingress_key.status = (
                    PublicIngressKeyStatus.ACTIVE
                    if request.status == LeadFormStatus.ACTIVE
                    else PublicIngressKeyStatus.PAUSED
                )
                ingress_key.allowed_origins = request.allowed_origins
                ingress_key.key_metadata = {
                    **(ingress_key.key_metadata or {}),
                    "form_name": form.name,
                }
                self.master_session.add(ingress_key)
                await self.master_session.flush()

            await self.org_session.commit()
            await self.master_session.commit()
            await self.org_session.refresh(form)
            if ingress_key:
                await self.master_session.refresh(ingress_key)
        except Exception:
            await self.org_session.rollback()
            await self.master_session.rollback()
            raise
        return form, updated_fields, ingress_key

    async def create_form(
        self, request: CreateLeadFormRequest
    ) -> tuple[LeadForm, list[LeadFormField], PublicIngressKey]:
        """Create a new form, materialize fields, and generate a unique public link key."""
        self._validate_field_requests(request.fields)
        try:
            actor_crm_user_id = await self._resolve_actor_crm_user_id()
            public_form_key = await self._generate_unique_public_key()
            form = LeadForm(
                organisation_id=self.organisation_id,
                name=request.name.strip(),
                description=request.description,
                status=request.status,
                current_version=1,
                settings=request.settings,
                created_by_user_id=actor_crm_user_id,
                updated_by_user_id=actor_crm_user_id,
            )
            self.org_session.add(form)
            await self.org_session.flush()

            fields = [
                LeadFormField(
                    organisation_id=self.organisation_id,
                    form_id=form.form_id,
                    form_version=form.current_version,
                    external_key=field.external_key.strip(),
                    label=field.label.strip(),
                    target_type=field.target_type,
                    target_key=field.target_key.strip() if field.target_key else None,
                    data_type=field.data_type,
                    required=field.required,
                    display_order=field.display_order if field.display_order is not None else index,
                    default_value=field.default_value,
                    validation=field.validation,
                )
                for index, field in enumerate(request.fields)
            ]
            for field in fields:
                self.org_session.add(field)
            await self.org_session.flush()

            ingress_key = PublicIngressKey(
                organisation_id=self.organisation_id,
                key_type=PublicIngressKeyType.PUBLIC_FORM,
                resource_type=PublicIngressResourceType.LEAD_FORM,
                resource_id=form.form_id,
                public_key=public_form_key,
                status=(
                    PublicIngressKeyStatus.ACTIVE
                    if request.status == LeadFormStatus.ACTIVE
                    else PublicIngressKeyStatus.PAUSED
                ),
                allowed_origins=request.allowed_origins,
                key_metadata={"form_name": form.name},
            )
            self.master_session.add(ingress_key)
            await self.master_session.flush()

            await self.org_session.commit()
            await self.master_session.commit()
            await self.org_session.refresh(form)
            await self.master_session.refresh(ingress_key)
        except Exception:
            await self.org_session.rollback()
            await self.master_session.rollback()
            raise
        return form, fields, ingress_key

    async def _resolve_actor_crm_user_id(self) -> UUID | None:
        """Return a crm_users FK for audit fields, creating it from the app user if needed."""
        if self.actor_user_id is None:
            return None

        user_service = InternalCRMUserService(
            org_session=self.org_session,
            master_session=self.master_session,
            organisation_id=self.organisation_id,
            actor_user_id=self.actor_user_id,
        )
        try:
            crm_user = await user_service.ensure_crm_user(self.actor_user_id)
        except InternalCRMUserServiceError as exc:
            raise LeadFormsServiceError(exc.message, exc.status_code) from exc
        return crm_user.user_id

    async def _get_form_or_404(self, form_id: UUID) -> LeadForm:
        stmt = select(LeadForm).where(
            col(LeadForm.organisation_id) == self.organisation_id,
            col(LeadForm.form_id) == form_id,
            col(LeadForm.is_deleted).is_(False),
        )
        form = (await self.org_session.execute(stmt)).scalars().first()
        if not form:
            raise LeadFormsServiceError("Lead form not found", status_code=404)
        return form

    async def _load_fields_by_form(self, form_ids: list[UUID]) -> dict[UUID, list[LeadFormField]]:
        stmt = (
            select(LeadFormField)
            .where(
                col(LeadFormField.form_id).in_(form_ids),
                col(LeadFormField.is_deleted).is_(False),
            )
            .order_by(col(LeadFormField.display_order).asc(), col(LeadFormField.created_at).asc())
        )
        fields = list((await self.org_session.execute(stmt)).scalars().all())
        grouped: dict[UUID, list[LeadFormField]] = {}
        for field in fields:
            grouped.setdefault(field.form_id, []).append(field)
        return grouped

    async def _load_fields(self, form_id: UUID) -> list[LeadFormField]:
        return (await self._load_fields_by_form([form_id])).get(form_id, [])

    async def _load_public_form_keys(self, form_ids: list[UUID]) -> dict[UUID, PublicIngressKey]:
        stmt = select(PublicIngressKey).where(
            col(PublicIngressKey.organisation_id) == self.organisation_id,
            col(PublicIngressKey.key_type) == PublicIngressKeyType.PUBLIC_FORM.value,
            col(PublicIngressKey.resource_type) == PublicIngressResourceType.LEAD_FORM.value,
            col(PublicIngressKey.resource_id).in_(form_ids),
            col(PublicIngressKey.status) != PublicIngressKeyStatus.REVOKED.value,
        )
        keys = list((await self.master_session.execute(stmt)).scalars().all())
        return {key.resource_id: key for key in keys}

    async def _generate_unique_public_key(self) -> str:
        for _ in range(10):
            key = (
                _PUBLIC_KEY_PREFIX
                + secrets.token_urlsafe(24).replace("-", "").replace("_", "")[:32]
            )
            exists = (
                (
                    await self.master_session.execute(
                        select(PublicIngressKey).where(col(PublicIngressKey.public_key) == key)
                    )
                )
                .scalars()
                .first()
            )
            if not exists:
                return key
        raise LeadFormsServiceError("Could not generate a unique form key", status_code=500)

    @staticmethod
    def _validate_field_requests(fields: list[Any]) -> None:
        external_keys: set[str] = set()
        for field in fields:
            external_key = field.external_key.strip()
            if external_key in external_keys:
                raise LeadFormsServiceError(
                    f"Duplicate form field key '{external_key}'",
                    status_code=400,
                )
            external_keys.add(external_key)
            if field.target_type == LeadFormFieldTargetType.LEAD_FIELD:
                if field.target_key not in _LEAD_FIELD_TARGETS:
                    raise LeadFormsServiceError(
                        f"Unsupported lead field mapping '{field.target_key}'",
                        status_code=400,
                    )
            elif field.target_type == LeadFormFieldTargetType.PROPERTY and not field.target_key:
                raise LeadFormsServiceError("Property mappings require target_key", status_code=400)

    @staticmethod
    def _apply_field_request(
        field: LeadFormField,
        field_request: Any,
        index: int,
        form_version: int,
    ) -> None:
        field.form_version = form_version
        field.external_key = field_request.external_key.strip()
        field.label = field_request.label.strip()
        field.target_type = field_request.target_type
        field.target_key = field_request.target_key.strip() if field_request.target_key else None
        field.data_type = field_request.data_type
        field.required = field_request.required
        field.display_order = (
            field_request.display_order if field_request.display_order is not None else index
        )
        field.default_value = field_request.default_value
        field.validation = field_request.validation
        field.is_deleted = False

    @staticmethod
    def _archive_removed_field(field: LeadFormField) -> None:
        field.is_deleted = True
        # The uniqueness index includes is_deleted, so archived rows still need unique keys.
        suffix = f"__deleted_{field.field_id.hex[:12]}"
        max_key_length = 128 - len(suffix)
        field.external_key = f"{field.external_key[:max_key_length]}{suffix}"


class PublicLeadFormSubmissionService:
    """Unauthenticated public submission flow for browser/server lead capture."""

    def __init__(
        self,
        org_session: AsyncSession,
        organisation_id: UUID,
        form_id: UUID,
        public_form_key: str,
        allowed_origins: list[str] | None = None,
    ):
        self.org_session = org_session
        self.organisation_id = organisation_id
        self.form_id = form_id
        self.public_form_key = public_form_key
        self.allowed_origins = allowed_origins or []

    async def load_definition(self, *, origin: str | None) -> PublicLeadFormDefinitionResponse:
        """Load a published form definition for public callers after origin validation."""
        form = await self._load_active_form()
        self._validate_origin(origin)
        fields = await self._load_active_fields(form.form_id)
        return PublicLeadFormDefinitionResponse(
            public_form_key=self.public_form_key,
            name=form.name,
            description=form.description,
            current_version=form.current_version,
            settings=form.settings,
            fields=[
                PublicLeadFormFieldItem(
                    external_key=field.external_key,
                    label=field.label,
                    data_type=field.data_type,
                    required=field.required,
                    display_order=field.display_order,
                    default_value=field.default_value,
                    validation=field.validation,
                )
                for field in fields
            ],
        )

    async def submit(
        self,
        *,
        fields_payload: dict[str, Any],
        context: dict[str, Any],
        origin: str | None,
        user_agent: str | None,
        submitter_ip: str | None,
    ) -> tuple[CRMLead, LeadFormSubmission]:
        """Validate public payload and persist both lead and form-submission audit rows."""
        return await self._submit_mapped_form(
            fields_payload=fields_payload,
            context=context,
            origin=origin,
            user_agent=user_agent,
            submitter_ip=submitter_ip,
            source_type=CRMLeadSourceType.FORM,
            source_ref=self.public_form_key,
            require_origin=True,
            ingestion_metadata=None,
        )

    async def submit_private(
        self,
        *,
        fields_payload: dict[str, Any],
        context: dict[str, Any],
        key_id: str,
        idempotency_key: str | None,
        user_agent: str | None,
        submitter_ip: str | None,
    ) -> tuple[CRMLead, LeadFormSubmission, bool]:
        """Submit through a form-scoped private key while preserving replay safety."""
        if not idempotency_key or not idempotency_key.strip():
            raise LeadFormsServiceError(
                "Idempotency-Key header is required",
                status_code=400,
                data={"code": "missing_idempotency_key"},
            )
        normalized_idempotency_key = idempotency_key.strip()
        self._validate_payload_shape(fields_payload)
        self._validate_payload_shape(context)
        existing = await self._find_existing_private_submission(
            key_id=key_id,
            idempotency_key=normalized_idempotency_key,
        )
        if existing:
            lead, submission = existing
            return lead, submission, True

        ingestion_metadata = {
            "key_id": key_id,
            "idempotency_key": normalized_idempotency_key,
            "received_at": datetime.now(UTC).isoformat(),
        }
        lead, submission = await self._submit_mapped_form(
            fields_payload=fields_payload,
            context=context,
            origin=None,
            user_agent=user_agent,
            submitter_ip=submitter_ip,
            source_type=CRMLeadSourceType.API,
            source_ref=key_id,
            require_origin=False,
            ingestion_metadata=ingestion_metadata,
        )
        return lead, submission, False

    async def _submit_mapped_form(
        self,
        *,
        fields_payload: dict[str, Any],
        context: dict[str, Any],
        origin: str | None,
        user_agent: str | None,
        submitter_ip: str | None,
        source_type: CRMLeadSourceType,
        source_ref: str,
        require_origin: bool,
        ingestion_metadata: dict[str, Any] | None,
    ) -> tuple[CRMLead, LeadFormSubmission]:
        """Create one submission audit row and one lead from a mapped form payload."""
        self._validate_payload_shape(fields_payload)
        self._validate_payload_shape(context)
        form = await self._load_active_form()
        if require_origin:
            self._validate_origin(origin)
        fields = await self._load_active_fields(form.form_id)
        mapped_lead, properties = self._map_payload(fields, fields_payload)
        now = datetime.now(UTC)
        submission_context = dict(context)
        if ingestion_metadata:
            submission_context["_copilotgtm"] = {"ingestion": ingestion_metadata}

        submission = LeadFormSubmission(
            organisation_id=self.organisation_id,
            form_id=form.form_id,
            form_version=form.current_version,
            raw_payload=fields_payload,
            mapped_payload=mapped_lead,
            context=submission_context,
            submitter_ip=submitter_ip,
            user_agent=user_agent,
            received_at=now,
        )
        self.org_session.add(submission)
        await self.org_session.flush()

        lead = CRMLead(
            **mapped_lead,
            organisation_id=self.organisation_id,
            source_type=source_type,
            source_name=form.name,
            source_ref=source_ref,
            status=CRMLeadStatus.NEW,
            form_id=form.form_id,
            form_version_id=None,
            form_submission_id=submission.submission_id,
            properties=properties,
            data_payload={
                "fields": fields_payload,
                "context": context,
                "form_id": str(form.form_id),
                "form_version": form.current_version,
                "public_form_key": self.public_form_key,
                **({"ingestion": ingestion_metadata} if ingestion_metadata else {}),
            },
            captured_at=now,
        )
        self.org_session.add(lead)
        await self.org_session.flush()
        submission.lead_id = lead.lead_id
        self.org_session.add(submission)
        await self.org_session.commit()
        await self.org_session.refresh(lead)
        await self.org_session.refresh(submission)
        return lead, submission

    async def _find_existing_private_submission(
        self,
        *,
        key_id: str,
        idempotency_key: str,
    ) -> tuple[CRMLead, LeadFormSubmission] | None:
        """Return the existing mapped private submission for a retry token, if present."""
        data_payload = cast(Any, CRMLead.data_payload)
        stmt = select(CRMLead).where(
            col(CRMLead.organisation_id) == self.organisation_id,
            col(CRMLead.is_deleted).is_(False),
            col(CRMLead.source_type) == CRMLeadSourceType.API.value,
            col(CRMLead.form_id) == self.form_id,
            data_payload["ingestion"]["key_id"].as_string() == key_id,
            data_payload["ingestion"]["idempotency_key"].as_string() == idempotency_key,
        )
        lead = (await self.org_session.execute(stmt)).scalars().first()
        if not lead or not lead.form_submission_id:
            return None
        submission_stmt = select(LeadFormSubmission).where(
            col(LeadFormSubmission.organisation_id) == self.organisation_id,
            col(LeadFormSubmission.submission_id) == lead.form_submission_id,
            col(LeadFormSubmission.form_id) == self.form_id,
            col(LeadFormSubmission.is_deleted).is_(False),
        )
        submission = (await self.org_session.execute(submission_stmt)).scalars().first()
        if not submission:
            return None
        return lead, submission

    async def _load_active_form(self) -> LeadForm:
        stmt = select(LeadForm).where(
            col(LeadForm.organisation_id) == self.organisation_id,
            col(LeadForm.form_id) == self.form_id,
            col(LeadForm.status) == LeadFormStatus.ACTIVE.value,
            col(LeadForm.is_deleted).is_(False),
        )
        form = (await self.org_session.execute(stmt)).scalars().first()
        if not form:
            raise LeadFormsServiceError("Lead form is not available", status_code=404)
        return form

    async def _load_active_fields(self, form_id: UUID) -> list[LeadFormField]:
        stmt = (
            select(LeadFormField)
            .where(
                col(LeadFormField.organisation_id) == self.organisation_id,
                col(LeadFormField.form_id) == form_id,
                col(LeadFormField.is_deleted).is_(False),
            )
            .order_by(col(LeadFormField.display_order).asc(), col(LeadFormField.created_at).asc())
        )
        return list((await self.org_session.execute(stmt)).scalars().all())

    def _validate_origin(self, origin: str | None) -> None:
        allowed = [item.rstrip("/") for item in self.allowed_origins if item]
        if not allowed:
            return
        if not origin or origin.rstrip("/") not in allowed:
            raise LeadFormsServiceError("Origin is not allowed for this form", status_code=403)

    def _map_payload(
        self,
        fields: list[LeadFormField],
        fields_payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        lead_payload: dict[str, Any] = {}
        properties: dict[str, Any] = {}
        known_keys = {field.external_key for field in fields}
        unknown_keys = set(fields_payload) - known_keys
        if unknown_keys:
            unknown_field = sorted(unknown_keys)[0]
            raise LeadFormsServiceError(
                f"Unknown field '{unknown_field}'",
                status_code=422,
                data={"code": "unknown_field", "field": unknown_field},
            )

        for field in fields:
            raw_value = fields_payload.get(field.external_key, field.default_value)
            if field.required and self._is_empty(raw_value):
                raise LeadFormsServiceError(
                    f"Missing required field '{field.external_key}'",
                    status_code=422,
                    data={"field": field.external_key},
                )
            if self._is_empty(raw_value):
                continue

            value = self._coerce_value(raw_value, field)
            if field.target_type == LeadFormFieldTargetType.LEAD_FIELD:
                if field.target_key in _LEAD_FIELD_TARGETS:
                    lead_payload[field.target_key] = value
            elif field.target_type == LeadFormFieldTargetType.PROPERTY:
                properties[field.target_key or field.external_key] = value

        if not any(lead_payload.get(key) for key in ("email", "phone", "full_name")):
            raise LeadFormsServiceError(
                "Submission must include at least one of email, phone, or name",
                status_code=422,
            )
        return lead_payload, properties

    def _coerce_value(self, value: Any, field: LeadFormField) -> Any:
        data_type = (
            field.data_type.value if hasattr(field.data_type, "value") else str(field.data_type)
        )
        if data_type == LeadFormFieldDataType.NUMBER.value:
            try:
                return float(value)
            except (TypeError, ValueError) as exc:
                raise LeadFormsServiceError(
                    f"Invalid number for field '{field.external_key}'",
                    status_code=422,
                    data={"field": field.external_key},
                ) from exc
        if data_type == LeadFormFieldDataType.BOOLEAN.value:
            if isinstance(value, bool):
                return value
            normalized = str(value).strip().lower()
            if normalized in {"true", "1", "yes", "y"}:
                return True
            if normalized in {"false", "0", "no", "n"}:
                return False
            raise LeadFormsServiceError(
                f"Invalid boolean for field '{field.external_key}'",
                status_code=422,
                data={"field": field.external_key},
            )
        if data_type in {LeadFormFieldDataType.EMAIL.value, LeadFormFieldDataType.TEXT.value}:
            return str(value).strip()
        return value

    @staticmethod
    def _is_empty(value: Any) -> bool:
        return value is None or (isinstance(value, str) and not value.strip())

    def _validate_payload_shape(self, payload: dict[str, Any]) -> None:
        for key, value in payload.items():
            if len(key) > MAX_PUBLIC_FIELD_KEY_LENGTH:
                raise LeadFormsServiceError(
                    "Submission field key is too long",
                    status_code=422,
                    data={"code": "invalid_field_key", "field": key[:MAX_PUBLIC_FIELD_KEY_LENGTH]},
                )
            self._validate_payload_value(value, field=key)

    def _validate_payload_value(self, value: Any, *, field: str) -> None:
        if isinstance(value, str) and len(value) > MAX_PUBLIC_SCALAR_STRING_LENGTH:
            raise LeadFormsServiceError(
                "Submission field value is too large",
                status_code=413,
                data={"code": "payload_too_large", "field": field},
            )
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                self._validate_payload_value(child_value, field=f"{field}.{child_key}")
        elif isinstance(value, list):
            for index, child_value in enumerate(value):
                self._validate_payload_value(child_value, field=f"{field}[{index}]")
