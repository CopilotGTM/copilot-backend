"""Gmail client helpers for users.messages.send."""

from __future__ import annotations

import base64
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

import httpx
from src.service.email_sends.models import (
    EmailSendAddress,
    EmailSendCreateRequest,
    EmailSendError,
    EmailSendProviderResult,
)
from src.service.integrations.api_error_utils import (
    IntegrationPermissionError,
    extract_api_error_details,
)
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)
from src.service.integrations.sync_health import AuthTokenRevokedError
from src.workers.integrations_worker.providers._google_api import GMAIL_BASE_URL


def _format_addresses(recipients: list[EmailSendAddress]) -> str:
    return ", ".join(
        formataddr((recipient.name or "", recipient.email)) if recipient.name else recipient.email
        for recipient in recipients
    )


def build_gmail_send_message_payload(request: EmailSendCreateRequest) -> dict[str, str]:
    """Build the base64url RFC 2822 payload expected by Gmail messages.send."""
    message = EmailMessage()
    if request.sender_email:
        message["From"] = request.sender_email
    message["To"] = _format_addresses(request.to)
    if request.cc:
        message["Cc"] = _format_addresses(request.cc)
    if request.bcc:
        message["Bcc"] = _format_addresses(request.bcc)
    message["Subject"] = request.subject
    subtype = "html" if request.body_content_type == "HTML" else "plain"
    message.set_content(request.body, subtype=subtype)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
    return {"raw": raw}


def _is_google_permission_error(response: httpx.Response) -> bool:
    response_text = response.text.lower()
    return response.status_code == 403 and (
        "insufficientpermissions" in response_text
        or "insufficient permissions" in response_text
        or "insufficient authentication scopes" in response_text
        or "access_token_scope_insufficient" in response_text
    )


async def gmail_api_post(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    json_body: dict[str, Any],
) -> dict[str, Any]:
    """Make a POST request to Gmail and return the parsed JSON response."""
    response = await client.post(f"{GMAIL_BASE_URL}{endpoint}", json=json_body)
    if _is_google_permission_error(response):
        raise IntegrationPermissionError(
            "Gmail send permission was not granted. Please reconnect and allow Gmail send access.",
            status_code=403,
            provider="google",
            endpoint=endpoint,
            response_summary=response.text,
        )
    response.raise_for_status()
    json_response = response.json()
    return json_response if isinstance(json_response, dict) else {}


class GmailEmailSendClient:
    """Send new Gmail messages through Gmail users.messages.send."""

    async def send_mail(
        self,
        token_info: IntegrationTokenInfo,
        request: EmailSendCreateRequest,
    ) -> EmailSendProviderResult:
        payload = build_gmail_send_message_payload(request)

        async def _request(access_token: str) -> dict[str, Any]:
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
                return await gmail_api_post(
                    client,
                    "/gmail/v1/users/me/messages/send",
                    json_body=payload,
                )

        try:
            response = await call_with_token_retry(token_info, _request)
        except IntegrationPermissionError as exc:
            raise EmailSendError(
                "email_send_scope_required",
                "Reconnect Gmail with mail send permission.",
                status_code=409,
            ) from exc
        except AuthTokenRevokedError as exc:
            raise EmailSendError(
                "email_reauth_required",
                "Reconnect Gmail before I can send emails.",
                status_code=409,
            ) from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                raise EmailSendError(
                    "email_reauth_required",
                    "Reconnect Gmail before I can send emails.",
                    status_code=409,
                ) from exc
            detail = extract_api_error_details(exc).summary
            suffix = f" Provider said: {detail}" if detail else ""
            raise EmailSendError(
                "email_send_provider_error",
                f"Gmail rejected the send request.{suffix}",
                status_code=502,
            ) from exc

        return EmailSendProviderResult(
            provider_status_code=200,
            provider_resource_id=str(response.get("id")) if response.get("id") else None,
        )
