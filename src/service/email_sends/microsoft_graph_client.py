"""Microsoft Graph client helpers for Outlook sendMail."""

from __future__ import annotations

from typing import Any

import httpx
from src.service.email_sends.models import EmailSendAddress, EmailSendCreateRequest, EmailSendError
from src.service.integrations.api_error_utils import IntegrationPermissionError
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)
from src.service.integrations.sync_health import AuthTokenRevokedError
from src.workers.integrations_worker.providers._microsoft_graph import graph_api_post_no_content


def _map_recipients(recipients: list[EmailSendAddress]) -> list[dict[str, dict[str, str]]]:
    return [
        {
            "emailAddress": {
                "address": recipient.email,
                "name": recipient.name or recipient.email,
            }
        }
        for recipient in recipients
    ]


def build_microsoft_send_mail_payload(request: EmailSendCreateRequest) -> dict[str, Any]:
    """Build a POST /me/sendMail payload for a new message."""
    message: dict[str, Any] = {
        "subject": request.subject,
        "body": {"contentType": request.body_content_type, "content": request.body},
        "toRecipients": _map_recipients(request.to),
    }
    if request.cc:
        message["ccRecipients"] = _map_recipients(request.cc)
    if request.bcc:
        message["bccRecipients"] = _map_recipients(request.bcc)

    return {
        "message": message,
        "saveToSentItems": request.save_to_sent_items,
    }


class MicrosoftEmailSendClient:
    """Send new Outlook messages through Microsoft Graph sendMail."""

    async def send_mail(
        self,
        token_info: IntegrationTokenInfo,
        request: EmailSendCreateRequest,
    ) -> None:
        payload = build_microsoft_send_mail_payload(request)

        async def _request(access_token: str) -> None:
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
                await graph_api_post_no_content(
                    client,
                    "/v1.0/me/sendMail",
                    json_body=payload,
                )

        try:
            await call_with_token_retry(token_info, _request)
        except IntegrationPermissionError as exc:
            raise EmailSendError(
                "email_send_scope_required",
                "Reconnect Outlook Email with mail send permission.",
                status_code=409,
            ) from exc
        except AuthTokenRevokedError as exc:
            raise EmailSendError(
                "email_reauth_required",
                "Reconnect Outlook Email before I can send emails.",
                status_code=409,
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise EmailSendError(
                "email_send_provider_error",
                "Outlook Email rejected the send request.",
                status_code=502,
            ) from exc
