"""Services for lead form configuration and public lead capture."""

from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from pydantic import EmailStr, TypeAdapter
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
    CRMCustomFieldEntity,
    CRMCustomFieldSource,
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
from src.service.crm.deals.contracts import DealWriteServiceError
from src.service.crm.deals.custom_field_service import DealCustomFieldWriteService
from src.service.crm.utils.crm_user_identity import (
    CRMUserIdentityResolutionError,
    ensure_app_user_crm_identity,
)

logger = get_logger("lead_forms_service")

_PUBLIC_KEY_PREFIX = "lf_"
_LEAD_FORM_PROPERTY_KEY_INVALID_RE = re.compile(r"[^a-z0-9_]+")
_EMAIL_ADAPTER = TypeAdapter(EmailStr)
_MAPPED_FORM_PAYLOAD_BYTES = 256_000
_MAPPED_FORM_PAYLOAD_DEPTH = 8
_MAPPED_FORM_PAYLOAD_LIST_ITEMS = 200
_MAPPED_FORM_PAYLOAD_NODES = 2000
_MAPPED_FORM_IDEMPOTENCY_KEY_LENGTH = 255
_MAPPED_FORM_KEY_ID_LENGTH = 128
_MAPPED_FORM_USER_AGENT_LENGTH = 1000
_MAPPED_FORM_SUBMITTER_IP_LENGTH = 64
_LEAD_PHONE_MAX_LENGTH = 64
_LEAD_FIELD_MAX_LENGTHS = {
    "first_name": 255,
    "last_name": 255,
    "full_name": 255,
    "email": 255,
    "phone": _LEAD_PHONE_MAX_LENGTH,
    "job_title": 255,
    "linkedin_url": 500,
    "company_name": 255,
    "company_domain": 255,
    "company_website": 500,
    "industry": 255,
    "country": 128,
    "region": 128,
    "city": 128,
    "interest": 255,
    "budget_currency": 3,
    "utm_source": 255,
    "utm_medium": 255,
    "utm_campaign": 255,
    "utm_term": 255,
    "utm_content": 255,
}
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
_LEAD_FIELD_COMPATIBLE_TYPES = {
    "first_name": {LeadFormFieldDataType.TEXT.value},
    "last_name": {LeadFormFieldDataType.TEXT.value},
    "full_name": {LeadFormFieldDataType.TEXT.value},
    "email": {LeadFormFieldDataType.EMAIL.value},
    "phone": {LeadFormFieldDataType.PHONE.value, LeadFormFieldDataType.TEXT.value},
    "job_title": {LeadFormFieldDataType.TEXT.value},
    "linkedin_url": {LeadFormFieldDataType.TEXT.value},
    "company_name": {LeadFormFieldDataType.TEXT.value},
    "company_domain": {LeadFormFieldDataType.TEXT.value},
    "company_website": {LeadFormFieldDataType.TEXT.value},
    "industry": {LeadFormFieldDataType.TEXT.value},
    "country": {LeadFormFieldDataType.TEXT.value},
    "region": {LeadFormFieldDataType.TEXT.value},
    "city": {LeadFormFieldDataType.TEXT.value},
    "message": {LeadFormFieldDataType.TEXT.value},
    "interest": {LeadFormFieldDataType.TEXT.value},
    "budget_amount": {LeadFormFieldDataType.NUMBER.value},
    "budget_currency": {LeadFormFieldDataType.TEXT.value},
    "utm_source": {LeadFormFieldDataType.TEXT.value},
    "utm_medium": {LeadFormFieldDataType.TEXT.value},
    "utm_campaign": {LeadFormFieldDataType.TEXT.value},
    "utm_term": {LeadFormFieldDataType.TEXT.value},
    "utm_content": {LeadFormFieldDataType.TEXT.value},
}
_LEAD_FORM_TO_CUSTOM_FIELD_TYPES = {
    LeadFormFieldDataType.TEXT.value: {
        CRMCustomFieldDataType.TEXT.value,
        CRMCustomFieldDataType.SELECT.value,
    },
    LeadFormFieldDataType.EMAIL.value: {CRMCustomFieldDataType.TEXT.value},
    LeadFormFieldDataType.PHONE.value: {CRMCustomFieldDataType.TEXT.value},
    LeadFormFieldDataType.NUMBER.value: {
        CRMCustomFieldDataType.INTEGER.value,
        CRMCustomFieldDataType.NUMBER.value,
        CRMCustomFieldDataType.CURRENCY.value,
        CRMCustomFieldDataType.PERCENTAGE.value,
    },
    LeadFormFieldDataType.BOOLEAN.value: {CRMCustomFieldDataType.BOOLEAN.value},
    LeadFormFieldDataType.DATE.value: {CRMCustomFieldDataType.DATE.value},
    LeadFormFieldDataType.DATETIME.value: {CRMCustomFieldDataType.DATETIME.value},
    LeadFormFieldDataType.JSON.value: {CRMCustomFieldDataType.MULTISELECT.value},
}


def _normalize_lead_form_property_key(value: str) -> str:
    """Mirror the property-key normalization used by the maintenance backfill script."""
    normalized = value.strip().lower()
    normalized = _LEAD_FORM_PROPERTY_KEY_INVALID_RE.sub("_", normalized)
    return normalized.strip("_")


class LeadFormsServiceError(Exception):
    """Public and authenticated lead form service error."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)


async def load_active_public_form_key(
    public_form_key: str,
    master_session: AsyncSession,
) -> PublicIngressKey | None:
    """Resolve an active public lead-form key from the master ingress registry."""
    registry_stmt = select(PublicIngressKey).where(
        col(PublicIngressKey.public_key) == public_form_key,
        col(PublicIngressKey.key_type) == PublicIngressKeyType.PUBLIC_FORM.value,
        col(PublicIngressKey.resource_type) == PublicIngressResourceType.LEAD_FORM.value,
        col(PublicIngressKey.status) == PublicIngressKeyStatus.ACTIVE.value,
    )
    return (await master_session.execute(registry_stmt)).scalars().first()


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
        await self._validate_property_field_requests(request.fields)
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
        await self._validate_property_field_requests(request.fields)
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

        try:
            crm_user = await ensure_app_user_crm_identity(
                org_session=self.org_session,
                master_session=self.master_session,
                organisation_id=self.organisation_id,
                app_user_id=self.actor_user_id,
                actor_user_id=self.actor_user_id,
            )
        except CRMUserIdentityResolutionError as exc:
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
        writable_targets: set[tuple[str, str]] = set()
        for field in fields:
            external_key = field.external_key.strip()
            if external_key in external_keys:
                raise LeadFormsServiceError(
                    f"Duplicate form field key '{external_key}'",
                    status_code=400,
                )
            external_keys.add(external_key)
            if field.target_type == LeadFormFieldTargetType.IGNORE and field.required:
                raise LeadFormsServiceError(
                    "Ignored fields cannot be required",
                    status_code=400,
                )
            if field.target_type == LeadFormFieldTargetType.LEAD_FIELD:
                target_key = field.target_key.strip() if field.target_key else None
                if target_key not in _LEAD_FIELD_TARGETS:
                    raise LeadFormsServiceError(
                        f"Unsupported lead field mapping '{field.target_key}'",
                        status_code=400,
                    )
                data_type = (
                    field.data_type.value
                    if hasattr(field.data_type, "value")
                    else str(field.data_type)
                )
                compatible_types = _LEAD_FIELD_COMPATIBLE_TYPES.get(target_key, set())
                if compatible_types and data_type not in compatible_types:
                    raise LeadFormsServiceError(
                        f"Unsupported data type '{data_type}' for lead field '{field.target_key}'",
                        status_code=400,
                    )
                target = (field.target_type.value, target_key)
            elif field.target_type == LeadFormFieldTargetType.PROPERTY:
                target_key = field.target_key.strip() if field.target_key else None
                if not target_key:
                    raise LeadFormsServiceError(
                        "Property mappings require target_key",
                        status_code=400,
                    )
                target = (
                    field.target_type.value,
                    _normalize_lead_form_property_key(target_key) or target_key,
                )
            else:
                continue

            if target in writable_targets:
                raise LeadFormsServiceError(
                    f"Duplicate form target mapping '{target[1]}'",
                    status_code=400,
                )
            writable_targets.add(target)

    async def _validate_property_field_requests(self, fields: list[Any]) -> None:
        property_fields = [
            field
            for field in fields
            if field.target_type == LeadFormFieldTargetType.PROPERTY and field.target_key
        ]
        if not property_fields:
            return

        target_key_aliases: dict[str, str] = {}
        target_keys: set[str] = set()
        for field in property_fields:
            target_key = field.target_key.strip()
            target_keys.add(target_key)
            normalized_key = _normalize_lead_form_property_key(target_key)
            if normalized_key:
                target_keys.add(normalized_key)
                target_key_aliases[target_key] = normalized_key
        stmt = select(CRMCustomFieldDefinition).where(
            col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
            col(CRMCustomFieldDefinition.entity_type) == CRMCustomFieldEntity.LEAD.value,
            col(CRMCustomFieldDefinition.field_source) == CRMCustomFieldSource.INTERNAL.value,
            col(CRMCustomFieldDefinition.is_active).is_(True),
            col(CRMCustomFieldDefinition.is_deleted).is_(False),
            col(CRMCustomFieldDefinition.key).in_(target_keys),
        )
        result = await self.org_session.execute(stmt)
        definitions = {field.key: field for field in result.scalars().all()}

        for field in property_fields:
            target_key = field.target_key.strip()
            definition = definitions.get(target_key)
            if definition is None:
                definition = definitions.get(target_key_aliases.get(target_key, ""))
            if definition is None:
                raise LeadFormsServiceError(
                    f"Unknown lead custom field '{field.target_key}'",
                    status_code=400,
                )
            form_type = (
                field.data_type.value if hasattr(field.data_type, "value") else str(field.data_type)
            )
            custom_type = (
                definition.data_type.value
                if hasattr(definition.data_type, "value")
                else str(definition.data_type)
            )
            if custom_type == CRMCustomFieldDataType.FORMULA.value:
                raise LeadFormsServiceError(
                    f"Lead custom field '{field.target_key}' is read-only",
                    status_code=400,
                )
            compatible_types = _LEAD_FORM_TO_CUSTOM_FIELD_TYPES.get(form_type, set())
            if custom_type not in compatible_types:
                raise LeadFormsServiceError(
                    f"Form field '{field.external_key}' data type '{form_type}' "
                    f"does not match lead custom field '{field.target_key}' type '{custom_type}'",
                    status_code=400,
                )

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
        lead_custom_fields = await self._load_lead_custom_field_definitions(fields)
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
                    options=self._public_field_options(field, lead_custom_fields),
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
        idempotency_key: str | None = None,
    ) -> tuple[CRMLead, LeadFormSubmission]:
        """Validate public payload and persist both lead and form-submission audit rows."""
        self._validate_payload_shape(fields_payload)
        self._validate_payload_shape(context)
        normalized_idempotency_key = self._normalize_idempotency_key(
            idempotency_key,
            required=False,
        )
        if normalized_idempotency_key:
            existing = await self._find_existing_public_submission(
                idempotency_key=normalized_idempotency_key
            )
            if existing:
                return existing

        try:
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
                ingestion_key_id=None,
                idempotency_key=normalized_idempotency_key,
            )
        except IntegrityError:
            if not normalized_idempotency_key:
                raise
            await self.org_session.rollback()
            existing = await self._find_existing_public_submission(
                idempotency_key=normalized_idempotency_key
            )
            if existing:
                return existing
            raise

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
        normalized_idempotency_key = self._normalize_idempotency_key(
            idempotency_key,
            required=True,
        )
        if len(key_id) > _MAPPED_FORM_KEY_ID_LENGTH:
            raise LeadFormsServiceError(
                "Private key id is too long",
                status_code=400,
                data={"code": "invalid_key_id"},
            )
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
        # Fast-path lookup protects normal retries. If no match is found, we try a
        # fresh write because concurrent retries can race before the first commit.
        try:
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
                ingestion_key_id=key_id,
                idempotency_key=normalized_idempotency_key,
            )
        except IntegrityError:
            # Unique violations are expected under race between retries; we rollback
            # and re-query so the winner row can be returned as a replay response.
            await self.org_session.rollback()
            existing = await self._find_existing_private_submission(
                key_id=key_id,
                idempotency_key=normalized_idempotency_key,
            )
            if existing:
                lead, submission = existing
                return lead, submission, True
            raise
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
        ingestion_key_id: str | None,
        idempotency_key: str | None,
    ) -> tuple[CRMLead, LeadFormSubmission]:
        """Create one submission audit row and one lead from a mapped form payload."""
        self._validate_payload_shape(fields_payload)
        self._validate_payload_shape(context)
        form = await self._load_active_form()
        if require_origin:
            self._validate_origin(origin)
        submitted_fields = (
            self._apply_public_spam_controls(form.settings, fields_payload, context)
            if source_type == CRMLeadSourceType.FORM
            else fields_payload
        )
        fields = await self._load_active_fields(form.form_id)
        lead_custom_fields = await self._load_lead_custom_field_definitions(fields)
        mapped_lead, properties = self._map_payload(
            fields,
            submitted_fields,
            lead_custom_fields=lead_custom_fields,
        )
        now = datetime.now(UTC)
        submission_context = dict(context)
        if ingestion_metadata:
            submission_context["_copilotgtm"] = {"ingestion": ingestion_metadata}
        if user_agent and len(user_agent) > _MAPPED_FORM_USER_AGENT_LENGTH:
            raise LeadFormsServiceError(
                "User-Agent header is too long",
                status_code=400,
                data={"code": "invalid_user_agent"},
            )
        if submitter_ip and len(submitter_ip) > _MAPPED_FORM_SUBMITTER_IP_LENGTH:
            raise LeadFormsServiceError(
                "Submitter IP is too long",
                status_code=400,
                data={"code": "invalid_submitter_ip"},
            )

        submission = LeadFormSubmission(
            organisation_id=self.organisation_id,
            form_id=form.form_id,
            form_version=form.current_version,
            ingestion_key_id=ingestion_key_id,
            idempotency_key=idempotency_key,
            raw_payload=submitted_fields,
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
            captured_at=now,
        )
        self.org_session.add(lead)
        # Insert submission and lead in separate flushes so both IDs are allocated
        # before the unique key constraint is evaluated and before commit.
        await self.org_session.flush()
        submission.lead_id = lead.lead_id
        self.org_session.add(submission)
        await self.org_session.commit()
        await self.org_session.refresh(lead)
        await self.org_session.refresh(submission)
        return lead, submission

    @staticmethod
    def _normalize_idempotency_key(idempotency_key: str | None, *, required: bool) -> str | None:
        normalized = idempotency_key.strip() if idempotency_key else None
        if not normalized:
            if required:
                raise LeadFormsServiceError(
                    "Idempotency-Key header is required",
                    status_code=400,
                    data={"code": "missing_idempotency_key"},
                )
            return None
        if len(normalized) > _MAPPED_FORM_IDEMPOTENCY_KEY_LENGTH:
            raise LeadFormsServiceError(
                "Idempotency-Key header is too long",
                status_code=400,
                data={"code": "invalid_idempotency_key"},
            )
        return normalized

    async def _find_existing_private_submission(
        self,
        *,
        key_id: str,
        idempotency_key: str,
    ) -> tuple[CRMLead, LeadFormSubmission] | None:
        """Return the existing mapped private submission for a retry token, if present."""
        submission_stmt = select(LeadFormSubmission).where(
            col(LeadFormSubmission.organisation_id) == self.organisation_id,
            col(LeadFormSubmission.form_id) == self.form_id,
            col(LeadFormSubmission.ingestion_key_id) == key_id,
            col(LeadFormSubmission.idempotency_key) == idempotency_key,
        )
        submission = (await self.org_session.execute(submission_stmt)).scalars().first()
        if not submission or not submission.lead_id:
            return None

        # Org + form/replay token is the trust boundary. If we load by the stored
        # pair and require the linked lead to still exist, we avoid stale JSON-based
        # reads and accidental cross-org replay collisions.

        lead_stmt = select(CRMLead).where(
            col(CRMLead.organisation_id) == self.organisation_id,
            col(CRMLead.lead_id) == submission.lead_id,
            col(CRMLead.form_id) == self.form_id,
            col(CRMLead.form_submission_id) == submission.submission_id,
            col(CRMLead.source_type) == CRMLeadSourceType.API.value,
        )
        lead = (await self.org_session.execute(lead_stmt)).scalars().first()
        if not lead:
            return None
        return lead, submission

    async def _find_existing_public_submission(
        self,
        *,
        idempotency_key: str,
    ) -> tuple[CRMLead, LeadFormSubmission] | None:
        """Return the existing public submission for a browser retry token, if present."""
        submission_stmt = select(LeadFormSubmission).where(
            col(LeadFormSubmission.organisation_id) == self.organisation_id,
            col(LeadFormSubmission.form_id) == self.form_id,
            col(LeadFormSubmission.ingestion_key_id).is_(None),
            col(LeadFormSubmission.idempotency_key) == idempotency_key,
        )
        submission = (await self.org_session.execute(submission_stmt)).scalars().first()
        if not submission or not submission.lead_id:
            return None

        lead_stmt = select(CRMLead).where(
            col(CRMLead.organisation_id) == self.organisation_id,
            col(CRMLead.lead_id) == submission.lead_id,
            col(CRMLead.form_id) == self.form_id,
            col(CRMLead.form_submission_id) == submission.submission_id,
            col(CRMLead.source_type) == CRMLeadSourceType.FORM.value,
        )
        lead = (await self.org_session.execute(lead_stmt)).scalars().first()
        if not lead:
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

    async def _load_lead_custom_field_definitions(
        self,
        fields: list[LeadFormField],
    ) -> dict[str, CRMCustomFieldDefinition]:
        target_key_aliases: dict[str, str] = {}
        target_keys: set[str] = set()
        for field in fields:
            if field.target_type != LeadFormFieldTargetType.PROPERTY or not field.target_key:
                continue
            target_keys.add(field.target_key)
            normalized_key = _normalize_lead_form_property_key(field.target_key)
            if normalized_key:
                target_keys.add(normalized_key)
                target_key_aliases[field.target_key] = normalized_key
        if not target_keys:
            return {}

        stmt = select(CRMCustomFieldDefinition).where(
            col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
            col(CRMCustomFieldDefinition.entity_type) == CRMCustomFieldEntity.LEAD.value,
            col(CRMCustomFieldDefinition.field_source) == CRMCustomFieldSource.INTERNAL.value,
            col(CRMCustomFieldDefinition.key).in_(target_keys),
        )
        result = await self.org_session.execute(stmt)
        definitions = {field.key: field for field in result.scalars().all()}
        for raw_key, normalized_key in target_key_aliases.items():
            if raw_key not in definitions and normalized_key in definitions:
                definitions[raw_key] = definitions[normalized_key]
        return definitions

    def _validate_origin(self, origin: str | None) -> None:
        allowed = [item.rstrip("/") for item in self.allowed_origins if item]
        if not allowed:
            return
        if not origin or origin.rstrip("/") not in allowed:
            raise LeadFormsServiceError("Origin is not allowed for this form", status_code=403)

    def _public_field_options(
        self,
        field: LeadFormField,
        lead_custom_fields: dict[str, CRMCustomFieldDefinition],
    ) -> dict[str, Any]:
        if field.target_type != LeadFormFieldTargetType.PROPERTY or not field.target_key:
            return {}
        definition = lead_custom_fields.get(field.target_key)
        if not definition or not definition.is_active or definition.is_deleted:
            return {}
        data_type = (
            definition.data_type.value
            if hasattr(definition.data_type, "value")
            else str(definition.data_type)
        )
        if data_type not in {
            CRMCustomFieldDataType.SELECT.value,
            CRMCustomFieldDataType.MULTISELECT.value,
        }:
            return {}

        options = definition.options if isinstance(definition.options, dict) else {}
        values = options.get("values")
        if not isinstance(values, list):
            return {}

        public_values: list[Any] = []
        for item in values:
            if isinstance(item, dict):
                if item.get("active") is False or item.get("is_active") is False:
                    continue
                if item.get("value") is not None:
                    public_values.append(item)
            elif item is not None:
                value = str(item).strip()
                if value:
                    public_values.append({"value": value, "label": value})
        return {"values": public_values} if public_values else {}

    def _apply_public_spam_controls(
        self,
        settings: dict[str, Any],
        fields_payload: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        spam_settings = settings.get("spam_protection") if isinstance(settings, dict) else None
        if not isinstance(spam_settings, dict):
            return fields_payload

        filtered_payload = dict(fields_payload)
        honeypot_field = spam_settings.get("honeypot_field")
        if isinstance(honeypot_field, str) and honeypot_field.strip():
            honeypot_key = honeypot_field.strip()
            if not self._is_empty(filtered_payload.get(honeypot_key)):
                raise LeadFormsServiceError(
                    "Submission failed spam checks",
                    status_code=422,
                    data={"code": "spam_rejected", "field": honeypot_key},
                )
            filtered_payload.pop(honeypot_key, None)

        min_fill_seconds = spam_settings.get("min_fill_seconds")
        if min_fill_seconds is None:
            return filtered_payload
        try:
            min_fill_seconds_value = float(min_fill_seconds)
        except (TypeError, ValueError):
            return filtered_payload
        if min_fill_seconds_value <= 0:
            return filtered_payload

        rendered_at = context.get("rendered_at") if isinstance(context, dict) else None
        try:
            parsed_rendered_at = datetime.fromisoformat(str(rendered_at).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise LeadFormsServiceError(
                "Submission failed spam checks",
                status_code=422,
                data={"code": "spam_rejected", "field": "rendered_at"},
            ) from exc
        if parsed_rendered_at.tzinfo is None:
            parsed_rendered_at = parsed_rendered_at.replace(tzinfo=UTC)
        if (datetime.now(UTC) - parsed_rendered_at).total_seconds() < min_fill_seconds_value:
            raise LeadFormsServiceError(
                "Submission failed spam checks",
                status_code=422,
                data={"code": "spam_rejected", "field": "rendered_at"},
            )
        return filtered_payload

    def _map_payload(
        self,
        fields: list[LeadFormField],
        fields_payload: dict[str, Any],
        *,
        lead_custom_fields: dict[str, CRMCustomFieldDefinition] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        lead_payload: dict[str, Any] = {}
        properties: dict[str, Any] = {}
        lead_custom_fields = lead_custom_fields or {}
        known_keys = {field.external_key for field in fields}
        unknown_keys = set(fields_payload) - known_keys
        if unknown_keys:
            unknown_field = sorted(unknown_keys)[0]
            raise LeadFormsServiceError(
                f"Unknown field '{unknown_field}'",
                status_code=422,
                data={"code": "unknown_field", "field": unknown_field},
            )

        effective_values: list[tuple[LeadFormField, Any]] = []
        effective_payload: dict[str, Any] = {}
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

            effective_values.append((field, raw_value))
            effective_payload[field.external_key] = raw_value

        self._validate_payload_shape(effective_payload)

        for field, raw_value in effective_values:
            value = self._coerce_value(raw_value, field)
            if field.target_type == LeadFormFieldTargetType.LEAD_FIELD:
                if field.target_key in _LEAD_FIELD_TARGETS:
                    if field.target_key == "company_domain":
                        value = self._normalize_domain(value)
                    self._validate_lead_field_length(field, value)
                    self._validate_lead_field_invariants(field, value)
                    lead_payload[field.target_key] = value
            elif field.target_type == LeadFormFieldTargetType.PROPERTY:
                target_key = field.target_key or field.external_key
                definition = lead_custom_fields.get(target_key)
                if definition is None:
                    normalized_key = _normalize_lead_form_property_key(target_key)
                    definition = lead_custom_fields.get(normalized_key)
                properties[target_key] = (
                    self._coerce_lead_custom_property(field, value, definition)
                    if definition is not None
                    else value
                )

        if not any(lead_payload.get(key) for key in ("email", "phone", "full_name")):
            raise LeadFormsServiceError(
                "Submission must include at least one of email, phone, or name",
                status_code=422,
            )
        return lead_payload, properties

    def _coerce_lead_custom_property(
        self,
        field: LeadFormField,
        value: Any,
        definition: CRMCustomFieldDefinition,
    ) -> Any:
        target_key = field.target_key or field.external_key
        if not definition.is_active or definition.is_deleted:
            raise LeadFormsServiceError(
                f"Lead custom field '{target_key}' is not available",
                status_code=422,
                data={"field": field.external_key},
            )

        form_type = (
            field.data_type.value if hasattr(field.data_type, "value") else str(field.data_type)
        )
        custom_type = (
            definition.data_type.value
            if hasattr(definition.data_type, "value")
            else str(definition.data_type)
        )
        if custom_type == CRMCustomFieldDataType.FORMULA.value:
            raise LeadFormsServiceError(
                f"Lead custom field '{target_key}' is read-only",
                status_code=422,
                data={"field": field.external_key},
            )
        compatible_types = _LEAD_FORM_TO_CUSTOM_FIELD_TYPES.get(form_type, set())
        if custom_type not in compatible_types:
            raise LeadFormsServiceError(
                f"Form field '{field.external_key}' data type '{form_type}' "
                f"does not match lead custom field '{target_key}' type '{custom_type}'",
                status_code=422,
                data={"field": field.external_key},
            )

        validator = DealCustomFieldWriteService(self.org_session, self.organisation_id)
        try:
            data_type = CRMCustomFieldDataType(custom_type)
            coerced_value = validator.coerce_custom_value(
                key=target_key,
                value=value,
                data_type=data_type,
            )
            if data_type in {CRMCustomFieldDataType.SELECT, CRMCustomFieldDataType.MULTISELECT}:
                validator.validate_option_membership(
                    key=target_key,
                    value=coerced_value,
                    field=definition,
                )
        except DealWriteServiceError as exc:
            raise LeadFormsServiceError(
                exc.message,
                status_code=422,
                data={"field": field.external_key},
            ) from exc
        return coerced_value

    def _coerce_value(self, value: Any, field: LeadFormField) -> Any:
        data_type = (
            field.data_type.value if hasattr(field.data_type, "value") else str(field.data_type)
        )
        if data_type == LeadFormFieldDataType.NUMBER.value:
            if isinstance(value, bool):
                raise LeadFormsServiceError(
                    f"Invalid number for field '{field.external_key}'",
                    status_code=422,
                    data={"field": field.external_key},
                )
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
        if data_type == LeadFormFieldDataType.EMAIL.value:
            if not isinstance(value, str):
                raise LeadFormsServiceError(
                    f"Invalid email for field '{field.external_key}'",
                    status_code=422,
                    data={"field": field.external_key},
                )
            try:
                return str(_EMAIL_ADAPTER.validate_python(value.strip())).lower()
            except Exception as exc:
                raise LeadFormsServiceError(
                    f"Invalid email for field '{field.external_key}'",
                    status_code=422,
                    data={"field": field.external_key},
                ) from exc
        if data_type == LeadFormFieldDataType.DATE.value:
            if not isinstance(value, str):
                raise LeadFormsServiceError(
                    f"Invalid date for field '{field.external_key}'",
                    status_code=422,
                    data={"field": field.external_key},
                )
            try:
                return date.fromisoformat(value.strip()).isoformat()
            except ValueError as exc:
                raise LeadFormsServiceError(
                    f"Invalid date for field '{field.external_key}'",
                    status_code=422,
                    data={"field": field.external_key},
                ) from exc
        if data_type == LeadFormFieldDataType.DATETIME.value:
            if not isinstance(value, str):
                raise LeadFormsServiceError(
                    f"Invalid datetime for field '{field.external_key}'",
                    status_code=422,
                    data={"field": field.external_key},
                )
            try:
                return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).isoformat()
            except ValueError as exc:
                raise LeadFormsServiceError(
                    f"Invalid datetime for field '{field.external_key}'",
                    status_code=422,
                    data={"field": field.external_key},
                ) from exc
        if data_type == LeadFormFieldDataType.JSON.value and not isinstance(
            value, dict | list | str | int | float | bool
        ):
            raise LeadFormsServiceError(
                f"Invalid JSON value for field '{field.external_key}'",
                status_code=422,
                data={"field": field.external_key},
            )
        if data_type == LeadFormFieldDataType.PHONE.value:
            normalized_phone = str(value).strip()
            if len(normalized_phone) > _LEAD_PHONE_MAX_LENGTH:
                raise LeadFormsServiceError(
                    f"Phone value is too long for field '{field.external_key}'",
                    status_code=422,
                    data={"field": field.external_key},
                )
            return normalized_phone
        if data_type == LeadFormFieldDataType.TEXT.value:
            return str(value).strip()
        return value

    @staticmethod
    def _is_empty(value: Any) -> bool:
        return value is None or (isinstance(value, str) and not value.strip())

    @staticmethod
    def _normalize_domain(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        domain = value.strip().lower()
        domain = domain.removeprefix("https://").removeprefix("http://")
        domain = domain.split("/", maxsplit=1)[0]
        return domain or None

    def _validate_lead_field_length(self, field: LeadFormField, value: Any) -> None:
        if not isinstance(value, str) or not field.target_key:
            return
        max_length = _LEAD_FIELD_MAX_LENGTHS.get(field.target_key)
        if max_length is None or len(value) <= max_length:
            return
        raise LeadFormsServiceError(
            f"Lead field value is too long for field '{field.external_key}'",
            status_code=422,
            data={"field": field.external_key},
        )

    def _validate_lead_field_invariants(self, field: LeadFormField, value: Any) -> None:
        if field.target_key == "budget_amount" and isinstance(value, int | float) and value < 0:
            raise LeadFormsServiceError(
                "Budget amount must be non-negative",
                status_code=422,
                data={"field": field.external_key},
            )
        if field.target_key == "budget_currency" and isinstance(value, str) and len(value) != 3:
            raise LeadFormsServiceError(
                "Budget currency must be a 3-character ISO code",
                status_code=422,
                data={"field": field.external_key},
            )

    def _validate_payload_shape(self, payload: dict[str, Any]) -> None:
        encoded_size = len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode(
                "utf-8"
            )
        )
        if encoded_size > _MAPPED_FORM_PAYLOAD_BYTES:
            raise LeadFormsServiceError(
                "Submission payload is too large",
                status_code=413,
                data={"code": "payload_too_large"},
            )
        node_count = 1
        for key, value in payload.items():
            if len(key) > MAX_PUBLIC_FIELD_KEY_LENGTH:
                raise LeadFormsServiceError(
                    "Submission field key is too long",
                    status_code=422,
                    data={"code": "invalid_field_key", "field": key[:MAX_PUBLIC_FIELD_KEY_LENGTH]},
                )
            node_count += self._validate_payload_value(value, field=key, depth=1)
            if node_count > _MAPPED_FORM_PAYLOAD_NODES:
                raise LeadFormsServiceError(
                    "Submission payload has too many fields",
                    status_code=413,
                    data={"code": "payload_too_complex"},
                )

    def _validate_payload_value(self, value: Any, *, field: str, depth: int) -> int:
        if depth > _MAPPED_FORM_PAYLOAD_DEPTH:
            raise LeadFormsServiceError(
                "Submission payload nesting is too deep",
                status_code=413,
                data={"code": "payload_too_deep", "field": field},
            )
        if isinstance(value, str) and len(value) > MAX_PUBLIC_SCALAR_STRING_LENGTH:
            raise LeadFormsServiceError(
                "Submission field value is too large",
                status_code=413,
                data={"code": "payload_too_large", "field": field},
            )
        if isinstance(value, dict):
            count = 1
            for child_key, child_value in value.items():
                count += self._validate_payload_value(
                    child_value, field=f"{field}.{child_key}", depth=depth + 1
                )
            return count
        if isinstance(value, list):
            if len(value) > _MAPPED_FORM_PAYLOAD_LIST_ITEMS:
                raise LeadFormsServiceError(
                    "Submission list is too large",
                    status_code=413,
                    data={"code": "payload_too_large", "field": field},
                )
            count = 1
            for index, child_value in enumerate(value):
                count += self._validate_payload_value(
                    child_value, field=f"{field}[{index}]", depth=depth + 1
                )
            return count
        return 1
