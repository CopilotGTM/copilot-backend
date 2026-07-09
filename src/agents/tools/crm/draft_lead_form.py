"""Draft lead-form agent tools."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.agents.tools.base import execute_with_capabilities
from src.contracts.crm.lead_form_models import (
    CreateLeadFormRequest,
    LeadFormFieldRequest,
    UpdateLeadFormRequest,
)
from src.domain.models.organisation_tables.sync.crm.lead_forms import (
    LeadFormFieldDataType,
    LeadFormFieldTargetType,
    LeadFormStatus,
)
from src.infra.database.sessions import acquire_master_async_session
from src.service.crm.leads.form_management_service import LeadFormsService, LeadFormsServiceError

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

_FIELD_KEY_INVALID_RE = re.compile(r"[^a-z0-9_]+")
_LEAD_FIELD_ALIASES = {
    "first_name": "first_name",
    "firstname": "first_name",
    "last_name": "last_name",
    "lastname": "last_name",
    "name": "full_name",
    "full_name": "full_name",
    "fullname": "full_name",
    "email": "email",
    "email_address": "email",
    "phone": "phone",
    "phone_number": "phone",
    "mobile": "phone",
    "job_title": "job_title",
    "title": "job_title",
    "linkedin": "linkedin_url",
    "linkedin_url": "linkedin_url",
    "company": "company_name",
    "company_name": "company_name",
    "company_domain": "company_domain",
    "domain": "company_domain",
    "company_website": "company_website",
    "website": "company_website",
    "industry": "industry",
    "country": "country",
    "region": "region",
    "state": "region",
    "city": "city",
    "message": "message",
    "comments": "message",
    "comment": "message",
    "interest": "interest",
    "budget": "budget_amount",
    "budget_amount": "budget_amount",
    "budget_currency": "budget_currency",
    "utm_source": "utm_source",
    "utm_medium": "utm_medium",
    "utm_campaign": "utm_campaign",
    "utm_term": "utm_term",
    "utm_content": "utm_content",
}
_LEAD_FIELD_TYPES = {
    "email": LeadFormFieldDataType.EMAIL,
    "phone": LeadFormFieldDataType.PHONE,
    "budget_amount": LeadFormFieldDataType.NUMBER,
}


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalize_key(value: str, fallback: str) -> str:
    cleaned = value.strip().lower()
    cleaned = _FIELD_KEY_INVALID_RE.sub("_", cleaned)
    cleaned = cleaned.strip("_")
    return cleaned or fallback


def _coerce_data_type(value: Any, fallback: LeadFormFieldDataType) -> LeadFormFieldDataType:
    if isinstance(value, LeadFormFieldDataType):
        return value
    if isinstance(value, str):
        try:
            return LeadFormFieldDataType(value.strip().lower())
        except ValueError:
            return fallback
    return fallback


def _lead_target_for(field: dict[str, Any], external_key: str, label: str) -> str | None:
    target_key = field.get("target_key")
    if isinstance(target_key, str):
        alias = _LEAD_FIELD_ALIASES.get(_normalize_key(target_key, external_key))
        if alias:
            return alias

    for candidate in (external_key, label):
        alias = _LEAD_FIELD_ALIASES.get(_normalize_key(candidate, external_key))
        if alias:
            return alias
    return None


def _build_field_requests(
    raw_fields: list[dict[str, Any]] | None,
) -> tuple[list[LeadFormFieldRequest], list[str]]:
    if not raw_fields:
        return [], ["At least one field is required before creating a draft form."]

    warnings: list[str] = []
    requests: list[LeadFormFieldRequest] = []
    used_keys: set[str] = set()

    for index, raw_field in enumerate(raw_fields):
        if not isinstance(raw_field, dict):
            warnings.append(f"Skipped field {index + 1}: expected an object.")
            continue

        label = _clean_optional(str(raw_field.get("label") or raw_field.get("name") or ""))
        if not label:
            warnings.append(f"Skipped field {index + 1}: missing label.")
            continue

        base_key = _normalize_key(str(raw_field.get("external_key") or label), f"field_{index + 1}")
        external_key = base_key
        suffix = 2
        while external_key in used_keys:
            external_key = f"{base_key}_{suffix}"
            suffix += 1
        used_keys.add(external_key)

        lead_target = _lead_target_for(raw_field, external_key, label)
        requested_type = _coerce_data_type(raw_field.get("data_type"), LeadFormFieldDataType.TEXT)
        required = bool(raw_field.get("required", False))

        if lead_target:
            data_type = _LEAD_FIELD_TYPES.get(lead_target, requested_type)
            requests.append(
                LeadFormFieldRequest(
                    external_key=external_key,
                    label=label,
                    target_type=LeadFormFieldTargetType.LEAD_FIELD,
                    target_key=lead_target,
                    data_type=data_type,
                    required=required,
                    display_order=index,
                )
            )
            continue

        warnings.append(
            f"Field '{label}' was added as unmapped and must be mapped before activation."
        )
        requests.append(
            LeadFormFieldRequest(
                external_key=external_key,
                label=label,
                target_type=LeadFormFieldTargetType.IGNORE,
                target_key=None,
                data_type=requested_type,
                required=False,
                display_order=index,
            )
        )
        if required:
            warnings.append(
                f"Field '{label}' was marked optional because unmapped fields cannot be required."
            )

    if not requests:
        warnings.append("No usable fields were provided.")
    return requests, warnings


def _field_request_from_model(field: Any) -> LeadFormFieldRequest:
    return LeadFormFieldRequest(
        external_key=field.external_key,
        label=field.label,
        target_type=field.target_type,
        target_key=field.target_key,
        data_type=field.data_type,
        required=field.required,
        display_order=field.display_order,
        default_value=field.default_value,
        validation=field.validation,
    )


def _field_result(field: Any) -> dict[str, Any]:
    return {
        "external_key": field.external_key,
        "label": field.label,
        "target_type": getattr(field.target_type, "value", field.target_type),
        "target_key": field.target_key,
        "data_type": getattr(field.data_type, "value", field.data_type),
        "required": field.required,
        "display_order": field.display_order,
    }


def _draft_form_result(form: Any, fields: list[Any], warnings: list[str]) -> dict[str, Any]:
    return {
        "status": "created" if getattr(form, "current_version", 1) == 1 else "updated",
        "form_id": str(form.form_id),
        "name": form.name,
        "form_status": getattr(form.status, "value", form.status),
        "current_version": form.current_version,
        "visibility": "private",
        "fields": [_field_result(field) for field in fields],
        "warnings": warnings,
        "message": f"Draft lead form **{form.name}** is ready for review.",
    }


def _public_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if not key.startswith("_")}


async def create_draft_lead_form_direct(
    *,
    context: AgentContext,
    name: str,
    fields: list[dict[str, Any]],
    description: str | None = None,
) -> dict[str, Any]:
    """Create a private-scoped lead form draft."""
    organisation_id = context.organisation_id
    if not organisation_id:
        return {"status": "error", "message": "Cannot create a form without an organization."}

    clean_name = _clean_optional(name)
    if not clean_name:
        return {"status": "error", "message": "Form name is required."}

    field_requests, warnings = _build_field_requests(fields)
    if not field_requests:
        return {"status": "error", "message": "At least one usable form field is required."}

    request = CreateLeadFormRequest(
        name=clean_name,
        description=_clean_optional(description),
        status=LeadFormStatus.DRAFT,
        allowed_origins=[],
        settings={"visibility": "private", "created_via": "agent_create_draft_lead_form"},
        fields=field_requests,
    )

    async for master_session in acquire_master_async_session():
        async with context.acquire_session() as org_session:
            service = LeadFormsService(
                org_session=org_session,
                master_session=master_session,
                organisation_id=organisation_id,
                actor_user_id=context.user_id,
            )
            try:
                form, saved_fields, _ingress_key = await service.create_form(request)
                return _draft_form_result(form, saved_fields, warnings)
            except LeadFormsServiceError as exc:
                await org_session.rollback()
                await master_session.rollback()
                return {"status": "error", "message": f"Could not create draft form: {exc.message}"}
            except Exception:
                await org_session.rollback()
                await master_session.rollback()
                logger.exception("create_draft_lead_form_direct: unexpected failure")
                return {"status": "error", "message": "Could not create draft form."}

    return {"status": "error", "message": "Could not acquire database session."}


async def update_draft_lead_form_direct(
    *,
    context: AgentContext,
    form_id: UUID | str,
    name: str | None = None,
    description: str | None = None,
    fields: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Update an existing lead form only while it remains a draft."""
    organisation_id = context.organisation_id
    if not organisation_id:
        return {"status": "error", "message": "Cannot update a form without an organization."}
    try:
        parsed_form_id = form_id if isinstance(form_id, UUID) else UUID(str(form_id))
    except ValueError:
        return {"status": "error", "message": "A valid form_id is required."}

    async for master_session in acquire_master_async_session():
        async with context.acquire_session() as org_session:
            service = LeadFormsService(
                org_session=org_session,
                master_session=master_session,
                organisation_id=organisation_id,
                actor_user_id=context.user_id,
            )
            try:
                current_form, current_fields, _current_key = await service.get_form(parsed_form_id)
                if current_form.status != LeadFormStatus.DRAFT:
                    return {
                        "status": "error",
                        "message": "Only draft lead forms can be edited by this tool.",
                    }

                if fields is None:
                    field_requests = [_field_request_from_model(field) for field in current_fields]
                    warnings: list[str] = []
                else:
                    field_requests, warnings = _build_field_requests(fields)
                    if not field_requests:
                        return {
                            "status": "error",
                            "message": "At least one usable form field is required.",
                        }

                request = UpdateLeadFormRequest(
                    name=_clean_optional(name) or current_form.name,
                    description=description
                    if description is not None
                    else current_form.description,
                    status=LeadFormStatus.DRAFT,
                    allowed_origins=[],
                    settings={
                        **(current_form.settings or {}),
                        "visibility": "private",
                        "updated_via": "agent_update_draft_lead_form",
                    },
                    fields=field_requests,
                )
                form, saved_fields, _ingress_key = await service.update_form(
                    parsed_form_id, request
                )
                return _draft_form_result(form, saved_fields, warnings)
            except LeadFormsServiceError as exc:
                await org_session.rollback()
                await master_session.rollback()
                return {"status": "error", "message": f"Could not update draft form: {exc.message}"}
            except Exception:
                await org_session.rollback()
                await master_session.rollback()
                logger.exception("update_draft_lead_form_direct: unexpected failure")
                return {"status": "error", "message": "Could not update draft form."}

    return {"status": "error", "message": "Could not acquire database session."}


def create_create_draft_lead_form(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory for the draft lead-form creation tool."""

    async def create_draft_lead_form_tool(
        name: str,
        fields: list[dict[str, Any]],
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create a private lead capture form as a draft for review before activation."""
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="CreateDraftLeadForm",
            tool_func=create_draft_lead_form_direct,
            parameters={
                "context": context,
                "name": name,
                "description": description,
                "fields": fields,
            },
            description=f"Creating draft lead form {name}".strip(),
            fallback_value={"status": "error", "message": "Could not create draft form."},
        )
        return _public_tool_result(result)

    return create_draft_lead_form_tool


def create_update_draft_lead_form(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory for the draft lead-form update tool."""

    async def update_draft_lead_form_tool(
        form_id: UUID | str,
        name: str | None = None,
        description: str | None = None,
        fields: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Edit an existing draft lead capture form without publishing it."""
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="UpdateDraftLeadForm",
            tool_func=update_draft_lead_form_direct,
            parameters={
                "context": context,
                "form_id": form_id,
                "name": name,
                "description": description,
                "fields": fields,
            },
            description=f"Updating draft lead form {form_id}",
            fallback_value={"status": "error", "message": "Could not update draft form."},
        )
        return _public_tool_result(result)

    return update_draft_lead_form_tool


__all__ = [
    "create_create_draft_lead_form",
    "create_draft_lead_form_direct",
    "create_update_draft_lead_form",
    "update_draft_lead_form_direct",
]
