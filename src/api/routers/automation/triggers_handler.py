"""Versioned HTTP endpoints for user-owned agent trigger definitions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import Response as FastAPIResponse
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import (
    enforce_automation_webhook_rate_limit,
    rate_limit_user_read,
    rate_limit_user_write,
)
from src.api.schemas.automation_trigger_models import (
    AutomationManualInvocationRequest,
    AutomationManualInvocationResponse,
    AutomationTriggerCatalog,
    AutomationTriggerCreateRequest,
    AutomationTriggerDetail,
    AutomationTriggerListResponse,
    AutomationTriggerRefreshRequest,
    AutomationTriggerUpdateRequest,
    AutomationWebhookCredentials,
    AutomationWebhookIngressResponse,
    AutomationWebhookRotateRequest,
)
from src.api.schemas.base_models import GenericResponse
from src.api.utils.http_response import Response
from src.domain.models.master_tables import UserAppRole
from src.domain.models.organisation_tables import AutomationTriggerStatus
from src.infra.config.configs import security_secrets_config
from src.infra.database.sessions import acquire_org_async_session
from src.infra.security.request_body import read_bounded_request_body
from src.infra.utils.logger import get_logger
from src.service.automation.errors import AutomationServiceError
from src.service.automation.triggers.service import AutomationTriggerService
from src.service.automation.triggers.webhook import (
    WEBHOOK_MAX_BODY_BYTES,
    WebhookIngressService,
    organisation_id_from_endpoint_token,
)

logger = get_logger("automation_triggers_handler")
_UNEXPECTED_ERROR_MESSAGE = "An unexpected error occurred while processing your request"

automation_triggers_router = APIRouter()
automation_webhooks_router = APIRouter()


async def get_automation_trigger_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
) -> AsyncIterator[AutomationTriggerService]:
    """Bind trigger access to authenticated organization and owner identity."""

    service = AutomationTriggerService(
        org_session,
        user_ids.organisation_id,
        user_ids.user_id,
        is_admin=user_ids.user_role == UserAppRole.ADMIN,
    )
    try:
        yield service
    finally:
        await service.close()


async def get_automation_webhook_service(
    endpoint_token: str,
) -> AsyncIterator[WebhookIngressService]:
    """Derive tenant and verification authority from the opaque public route."""

    try:
        organisation_id = organisation_id_from_endpoint_token(endpoint_token)
        root_secret = await security_secrets_config.get_automation_webhook_root_secret()
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(
            status_code=404, detail="Automation webhook endpoint not found"
        ) from exc
    async for session in acquire_org_async_session(organisation_id):
        yield WebhookIngressService(
            session,
            organisation_id,
            endpoint_token,
            root_secret=root_secret,
            previous_root_secrets=(
                security_secrets_config.get_automation_webhook_previous_root_secrets()
            ),
        )
        return
    raise HTTPException(status_code=503, detail="Automation webhook service unavailable")


def rate_limit_automation_webhook(request: Request, endpoint_token: str) -> None:
    """Apply the public-ingress quota before opening a tenant session."""

    enforce_automation_webhook_rate_limit(request, endpoint_token)


@automation_webhooks_router.post(
    "/{endpoint_token}",
    response_model=GenericResponse[AutomationWebhookIngressResponse],
    status_code=http_status.HTTP_202_ACCEPTED,
    summary="Receive signed automation webhook",
)
async def receive_automation_webhook(
    endpoint_token: str,
    request: Request,
    response: FastAPIResponse,
    _rate_limit: None = Depends(rate_limit_automation_webhook),
    service: WebhookIngressService = Depends(get_automation_webhook_service),
) -> GenericResponse[AutomationWebhookIngressResponse]:
    """Verify exact request bytes and atomically enqueue one direct event."""

    _ = endpoint_token  # FastAPI binds the same token into the service dependency.
    raw_body = await read_bounded_request_body(
        request,
        max_bytes=WEBHOOK_MAX_BODY_BYTES,
        label="Automation webhook",
    )
    try:
        item = await service.ingest(
            raw_body=raw_body,
            timestamp=request.headers.get("x-copilotgtm-timestamp", ""),
            signature=request.headers.get("x-copilotgtm-signature", ""),
            dedupe_key=request.headers.get("x-copilotgtm-delivery-id"),
        )
        await service.session.commit()
        return Response.accepted_response(
            "Automation webhook accepted",
            data=AutomationWebhookIngressResponse(event_id=item.envelope.event_id),
            response=response,
        )
    except AutomationServiceError as exc:
        await service.session.rollback()
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        await service.session.rollback()
        logger.exception("Unexpected automation webhook ingress failure")
        return Response.internal_server_error_response(_UNEXPECTED_ERROR_MESSAGE, response=response)


@automation_triggers_router.get(
    "/automation-trigger-catalog",
    response_model=GenericResponse[AutomationTriggerCatalog],
    summary="Get automation trigger catalog",
)
async def get_automation_trigger_catalog(
    response: FastAPIResponse,
    service: AutomationTriggerService = Depends(get_automation_trigger_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[AutomationTriggerCatalog]:
    """Return supported trigger contracts and organization CRM options."""

    try:
        return Response.success_response(
            "Trigger catalog retrieved successfully", data=await service.get_catalog()
        )
    except Exception:
        logger.exception("Unexpected error retrieving trigger catalog")
        return Response.internal_server_error_response(_UNEXPECTED_ERROR_MESSAGE, response=response)


@automation_triggers_router.post(
    "/agents/{agent_id}/triggers",
    response_model=GenericResponse[AutomationTriggerDetail],
    status_code=http_status.HTTP_201_CREATED,
    summary="Create agent trigger",
)
async def create_agent_trigger(
    agent_id: UUID,
    body: AutomationTriggerCreateRequest,
    response: FastAPIResponse,
    service: AutomationTriggerService = Depends(get_automation_trigger_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[AutomationTriggerDetail]:
    """Create and pin one validated trigger in a single transaction."""

    try:
        item = await service.create_trigger(agent_id, body)
        await service.session.commit()
        response.headers["Location"] = f"/users/v1/agents/{agent_id}/triggers/{item.trigger_id}"
        return Response.created_response(
            "Trigger created successfully", data=item, response=response
        )
    except AutomationServiceError as exc:
        await service.session.rollback()
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        await service.session.rollback()
        logger.exception("Unexpected error creating automation trigger")
        return Response.internal_server_error_response(_UNEXPECTED_ERROR_MESSAGE, response=response)


@automation_triggers_router.get(
    "/agents/{agent_id}/triggers",
    response_model=GenericResponse[AutomationTriggerListResponse],
    summary="List agent triggers",
)
async def list_agent_triggers(
    agent_id: UUID,
    response: FastAPIResponse,
    status: AutomationTriggerStatus | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    service: AutomationTriggerService = Depends(get_automation_trigger_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[AutomationTriggerListResponse]:
    """List non-archived triggers for one owned agent."""

    try:
        data = await service.list_triggers(agent_id, status=status, limit=limit, offset=offset)
        return Response.success_response("Triggers retrieved successfully", data=data)
    except AutomationServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        logger.exception("Unexpected error listing automation triggers")
        return Response.internal_server_error_response(_UNEXPECTED_ERROR_MESSAGE, response=response)


@automation_triggers_router.get(
    "/agents/{agent_id}/triggers/{trigger_id}",
    response_model=GenericResponse[AutomationTriggerDetail],
    summary="Get agent trigger",
)
async def get_agent_trigger(
    agent_id: UUID,
    trigger_id: UUID,
    response: FastAPIResponse,
    service: AutomationTriggerService = Depends(get_automation_trigger_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[AutomationTriggerDetail]:
    """Return one owned trigger with its pinned target snapshot."""

    try:
        return Response.success_response(
            "Trigger retrieved successfully",
            data=await service.get_trigger(agent_id, trigger_id),
        )
    except AutomationServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        logger.exception("Unexpected error retrieving automation trigger")
        return Response.internal_server_error_response(_UNEXPECTED_ERROR_MESSAGE, response=response)


@automation_triggers_router.post(
    "/agents/{agent_id}/triggers/{trigger_id}/invoke",
    response_model=GenericResponse[AutomationManualInvocationResponse],
    summary="Invoke agent trigger",
)
async def invoke_agent_trigger(
    agent_id: UUID,
    trigger_id: UUID,
    body: AutomationManualInvocationRequest,
    response: FastAPIResponse,
    service: AutomationTriggerService = Depends(get_automation_trigger_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[AutomationManualInvocationResponse]:
    """Atomically enqueue a global or subject-isolated manual invocation."""

    try:
        item = await service.invoke_manual_trigger(agent_id, trigger_id, body)
        await service.session.commit()
        return Response.success_response("Trigger invoked successfully", data=item)
    except AutomationServiceError as exc:
        await service.session.rollback()
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        await service.session.rollback()
        logger.exception("Unexpected error invoking automation trigger")
        return Response.internal_server_error_response(_UNEXPECTED_ERROR_MESSAGE, response=response)


@automation_triggers_router.post(
    "/agents/{agent_id}/triggers/{trigger_id}/rotate-webhook-secret",
    response_model=GenericResponse[AutomationWebhookCredentials],
    summary="Rotate agent webhook secret",
)
async def rotate_agent_webhook_secret(
    agent_id: UUID,
    trigger_id: UUID,
    body: AutomationWebhookRotateRequest,
    response: FastAPIResponse,
    service: AutomationTriggerService = Depends(get_automation_trigger_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[AutomationWebhookCredentials]:
    """Rotate a signing secret and return its plaintext value exactly once."""

    try:
        credentials = await service.rotate_webhook_secret(agent_id, trigger_id, body)
        await service.session.commit()
        return Response.success_response(
            "Webhook secret rotated successfully",
            data=credentials,
        )
    except AutomationServiceError as exc:
        await service.session.rollback()
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        await service.session.rollback()
        logger.exception("Unexpected error rotating automation webhook secret")
        return Response.internal_server_error_response(_UNEXPECTED_ERROR_MESSAGE, response=response)


@automation_triggers_router.put(
    "/agents/{agent_id}/triggers/{trigger_id}",
    response_model=GenericResponse[AutomationTriggerDetail],
    summary="Replace agent trigger",
)
async def replace_agent_trigger(
    agent_id: UUID,
    trigger_id: UUID,
    body: AutomationTriggerUpdateRequest,
    response: FastAPIResponse,
    service: AutomationTriggerService = Depends(get_automation_trigger_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[AutomationTriggerDetail]:
    """Replace configuration while preserving the explicit agent revision pin."""

    try:
        item = await service.replace_trigger(agent_id, trigger_id, body)
        await service.session.commit()
        return Response.success_response("Trigger updated successfully", data=item)
    except AutomationServiceError as exc:
        await service.session.rollback()
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        await service.session.rollback()
        logger.exception("Unexpected error replacing automation trigger")
        return Response.internal_server_error_response(_UNEXPECTED_ERROR_MESSAGE, response=response)


@automation_triggers_router.post(
    "/agents/{agent_id}/triggers/{trigger_id}/refresh-target",
    response_model=GenericResponse[AutomationTriggerDetail],
    summary="Refresh trigger target",
)
async def refresh_agent_trigger_target(
    agent_id: UUID,
    trigger_id: UUID,
    body: AutomationTriggerRefreshRequest,
    response: FastAPIResponse,
    service: AutomationTriggerService = Depends(get_automation_trigger_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[AutomationTriggerDetail]:
    """Explicitly repin one trigger to the agent's current revision."""

    try:
        item = await service.refresh_target(
            agent_id, trigger_id, expected_updated_at=body.expected_updated_at
        )
        await service.session.commit()
        return Response.success_response("Trigger target refreshed successfully", data=item)
    except AutomationServiceError as exc:
        await service.session.rollback()
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        await service.session.rollback()
        logger.exception("Unexpected error refreshing automation trigger target")
        return Response.internal_server_error_response(_UNEXPECTED_ERROR_MESSAGE, response=response)


@automation_triggers_router.delete(
    "/agents/{agent_id}/triggers/{trigger_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Archive agent trigger",
)
async def archive_agent_trigger(
    agent_id: UUID,
    trigger_id: UUID,
    response: FastAPIResponse,
    expected_updated_at: datetime = Query(),
    service: AutomationTriggerService = Depends(get_automation_trigger_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> FastAPIResponse | GenericResponse[None]:
    """Soft-delete one trigger using optimistic concurrency."""

    try:
        if expected_updated_at.tzinfo is None or expected_updated_at.utcoffset() is None:
            raise AutomationServiceError("expected_updated_at must include timezone")
        await service.archive_trigger(agent_id, trigger_id, expected_updated_at=expected_updated_at)
        await service.session.commit()
        return Response.no_content_response()
    except AutomationServiceError as exc:
        await service.session.rollback()
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        await service.session.rollback()
        logger.exception("Unexpected error archiving automation trigger")
        return Response.internal_server_error_response(_UNEXPECTED_ERROR_MESSAGE, response=response)
