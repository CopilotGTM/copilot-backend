"""CRM custom field definition API handler."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_custom_field_models import (
    CreateCustomFieldRequest,
    CustomFieldDefinitionItem,
    UpdateCustomFieldRequest,
    ValidateFormulaRequest,
    ValidateFormulaResponse,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.sync.crm.custom_fields import CRMCustomFieldEntity
from src.infra.utils.logger import get_logger
from src.service.crm.internal.custom_field_service import (
    CustomFieldService,
    CustomFieldServiceError,
)

logger = get_logger(__name__)

_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

custom_fields_router = APIRouter()


async def get_custom_field_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    service = CustomFieldService(
        session=session,
        organisation_id=user_ids.organisation_id,
        actor_user_id=user_ids.user_id,
    )
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("CustomFieldService close failed", exc_info=True)


def _to_response_item(field) -> CustomFieldDefinitionItem:
    return CustomFieldDefinitionItem(
        field_id=field.field_id,
        entity=field.entity_type,
        key=field.key,
        label=field.label,
        data_type=field.data_type,
        required=field.required,
        is_predefined=field.is_predefined,
        is_active=field.is_active,
        display_order=field.display_order,
        description=field.description,
        options=field.options or {},
        validation=field.validation or {},
        formula=field.formula,
        created_at=field.created_at,
        updated_at=field.updated_at,
    )


@custom_fields_router.get(
    "/custom-fields",
    response_model=GenericResponse[list[CustomFieldDefinitionItem]],
    summary="List custom field definitions",
)
async def list_custom_fields(
    response: FastAPIResponse,
    entity: CRMCustomFieldEntity = Query(
        CRMCustomFieldEntity.DEAL, description="Entity scope for field definitions"
    ),
    include_inactive: bool = Query(False, description="Include inactive fields in the response"),
    service: CustomFieldService = Depends(get_custom_field_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[list[CustomFieldDefinitionItem]]:
    """List custom field definitions for the current organisation."""
    try:
        fields = await service.list_fields(entity=entity, include_inactive=include_inactive)
        items = [_to_response_item(field) for field in fields]
        return Response.success_response(message="Custom fields retrieved", data=items)
    except CustomFieldServiceError as exc:
        logger.warning("Custom field list error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message,
            status_code=exc.status_code,
            response=response,
        )
    except Exception as exc:
        logger.error("Unexpected custom field list error: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
            response=response,
        )


@custom_fields_router.post(
    "/custom-fields",
    response_model=GenericResponse[CustomFieldDefinitionItem],
    status_code=201,
    summary="Create a custom field definition",
)
async def create_custom_field(
    request: CreateCustomFieldRequest,
    response: FastAPIResponse,
    service: CustomFieldService = Depends(get_custom_field_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[CustomFieldDefinitionItem]:
    """Create a custom field definition for the current organisation."""
    try:
        field = await service.create_field(request.model_dump())
        await service.session.commit()
        await service.session.refresh(field)
        return Response.created_response(
            message="Custom field created",
            data=_to_response_item(field),
            response=response,
        )
    except CustomFieldServiceError as exc:
        await service.session.rollback()
        logger.warning("Custom field create error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message,
            status_code=exc.status_code,
            response=response,
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected custom field create error: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
            response=response,
        )


@custom_fields_router.patch(
    "/custom-fields/{field_id}",
    response_model=GenericResponse[CustomFieldDefinitionItem],
    summary="Update a custom field definition",
)
async def update_custom_field(
    field_id: UUID,
    request: UpdateCustomFieldRequest,
    response: FastAPIResponse,
    service: CustomFieldService = Depends(get_custom_field_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[CustomFieldDefinitionItem]:
    """Update mutable properties of a custom field definition."""
    try:
        field = await service.update_field(field_id, request.model_dump(exclude_unset=True))
        await service.session.commit()
        await service.session.refresh(field)
        return Response.success_response(
            message="Custom field updated",
            data=_to_response_item(field),
        )
    except CustomFieldServiceError as exc:
        await service.session.rollback()
        logger.warning("Custom field update error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message,
            status_code=exc.status_code,
            response=response,
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected custom field update error: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
            response=response,
        )


@custom_fields_router.delete(
    "/custom-fields/{field_id}",
    response_model=GenericResponse[CustomFieldDefinitionItem],
    summary="Archive a custom field definition",
)
async def delete_custom_field(
    field_id: UUID,
    response: FastAPIResponse,
    service: CustomFieldService = Depends(get_custom_field_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[CustomFieldDefinitionItem]:
    """Soft delete a custom field definition."""
    try:
        field = await service.archive_field(field_id)
        await service.session.commit()
        await service.session.refresh(field)
        return Response.success_response(
            message="Custom field archived",
            data=_to_response_item(field),
        )
    except CustomFieldServiceError as exc:
        await service.session.rollback()
        logger.warning("Custom field archive error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message,
            status_code=exc.status_code,
            response=response,
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected custom field archive error: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
            response=response,
        )


@custom_fields_router.post(
    "/custom-fields/validate-formula",
    response_model=GenericResponse[ValidateFormulaResponse],
    summary="Validate formula expression",
)
async def validate_formula(
    request: ValidateFormulaRequest,
    response: FastAPIResponse,
    service: CustomFieldService = Depends(get_custom_field_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[ValidateFormulaResponse]:
    """Validate formula syntax and field dependencies for the current organisation."""
    try:
        dependencies = await service.validate_formula(
            entity=request.entity,
            formula=request.formula,
        )
        payload = ValidateFormulaResponse(is_valid=True, dependencies=dependencies)
        return Response.success_response(message="Formula is valid", data=payload)
    except CustomFieldServiceError as exc:
        logger.warning("Formula validation error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message,
            status_code=exc.status_code,
            response=response,
        )
    except Exception as exc:
        logger.error("Unexpected formula validation error: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
            response=response,
        )
