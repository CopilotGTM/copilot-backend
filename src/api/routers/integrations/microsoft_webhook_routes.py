"""Public Microsoft Graph webhook endpoints.

Called by Microsoft Graph subscription callbacks for both data-change and
lifecycle events. Routing parameters (`organisation_id`, `integration_id`,
`kind`) are part of our tenant boundary and must be present before we enqueue
any downstream sync work.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import PlainTextResponse
from src.service.integrations.webhooks.microsoft.request_service import (
    MICROSOFT_WEBHOOK_MAX_BODY_BYTES,
    MicrosoftWebhookRequestService,
)

router = APIRouter(prefix="/microsoft", tags=["integrations", "microsoft-webhooks"])


async def get_microsoft_webhook_request_service() -> MicrosoftWebhookRequestService:
    """Provide a fresh request service instance for each webhook request."""
    return MicrosoftWebhookRequestService()


@router.post("/webhook")
async def handle_microsoft_webhook(
    request: Request,
    organisation_id: UUID | None = Query(default=None),
    integration_id: UUID | None = Query(default=None),
    kind: str | None = Query(default=None),
    validation_token: str | None = Query(default=None, alias="validationToken"),
    webhook_service: MicrosoftWebhookRequestService = Depends(
        get_microsoft_webhook_request_service
    ),
) -> Response:
    """Handle Microsoft Graph data-change webhook callbacks.

    Validation callbacks must echo `validationToken` exactly or Microsoft will
    reject the subscription handshake.
    """
    if validation_token is not None:
        return PlainTextResponse(validation_token, status_code=status.HTTP_200_OK)

    await webhook_service.handle_webhook(
        request=request,
        organisation_id=organisation_id,
        integration_id=integration_id,
        kind=kind,
    )
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/webhook/lifecycle")
async def handle_microsoft_webhook_lifecycle(
    request: Request,
    organisation_id: UUID | None = Query(default=None),
    integration_id: UUID | None = Query(default=None),
    kind: str | None = Query(default=None),
    validation_token: str | None = Query(default=None, alias="validationToken"),
    webhook_service: MicrosoftWebhookRequestService = Depends(
        get_microsoft_webhook_request_service
    ),
) -> Response:
    """Handle Microsoft Graph lifecycle callbacks (renewal/missed notifications)."""
    if validation_token is not None:
        return PlainTextResponse(validation_token, status_code=status.HTTP_200_OK)

    await webhook_service.handle_lifecycle(
        request=request,
        organisation_id=organisation_id,
        integration_id=integration_id,
        kind=kind,
    )
    return Response(status_code=status.HTTP_202_ACCEPTED)


__all__ = [
    "MICROSOFT_WEBHOOK_MAX_BODY_BYTES",
    "get_microsoft_webhook_request_service",
    "router",
]
