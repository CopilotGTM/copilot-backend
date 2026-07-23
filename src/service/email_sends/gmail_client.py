"""Gmail client helpers for users.messages.send."""

from __future__ import annotations

import base64
from email import message_from_bytes, policy
from email.message import EmailMessage, Message
from email.utils import formataddr, getaddresses
from typing import Any

import httpx
from src.service.email_sends.models import (
    EmailDraftSaveRequest,
    EmailDraftSnapshot,
    EmailSendAddress,
    EmailSendCreateRequest,
    EmailSendError,
    EmailSendProviderResult,
    email_draft_content_hash,
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
    """Build Gmail MIME, including thread headers for a verified reply draft."""

    message = EmailMessage()
    if request.sender_email:
        message["From"] = request.sender_email
    message["To"] = _format_addresses(request.to)
    if request.cc:
        message["Cc"] = _format_addresses(request.cc)
    if request.bcc:
        message["Bcc"] = _format_addresses(request.bcc)
    message["Subject"] = request.subject
    if isinstance(request, EmailDraftSaveRequest) and request.reply_to_internet_message_id:
        message["In-Reply-To"] = request.reply_to_internet_message_id
        message["References"] = " ".join(request.reply_references)
    subtype = "html" if request.body_content_type == "HTML" else "plain"
    message.set_content(request.body, subtype=subtype)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
    payload = {"raw": raw}
    if isinstance(request, EmailDraftSaveRequest) and request.reply_thread_id:
        # Gmail requires both the provider thread ID and RFC reply headers; one
        # without the other can silently create a separate conversation.
        payload["threadId"] = request.reply_thread_id
    return payload


def _draft_addresses(message: Message, header: str) -> list[EmailSendAddress]:
    return [
        EmailSendAddress(email=email, name=name or None)
        for name, email in getaddresses(message.get_all(header, []))
        if email
    ]


def _draft_body(message: Message) -> tuple[str, str]:
    """Extract one editable body while preserving whether the provider stored HTML."""

    if message.is_multipart():
        preferred = message.get_body(preferencelist=("html", "plain"))
        if preferred is None:
            return "", "Text"
        content = preferred.get_content()
        return str(content), "HTML" if preferred.get_content_subtype() == "html" else "Text"
    return str(message.get_content()), "HTML" if message.get_content_subtype() == "html" else "Text"


def _decode_gmail_raw_message(payload: dict[str, Any]) -> Message:
    """Decode one Gmail message payload so draft state can be reviewed or preserved."""

    encoded = str(payload.get("raw") or "")
    encoded += "=" * (-len(encoded) % 4)
    return message_from_bytes(base64.urlsafe_b64decode(encoded), policy=policy.default)


def preserve_gmail_draft_thread(
    message_payload: dict[str, str],
    existing_draft: dict[str, Any],
) -> dict[str, str]:
    """Carry provider thread identity across Gmail's replace-on-update operation.

    Gmail destroys and recreates the message inside a draft during update. A
    body edit therefore has to copy the existing thread ID and RFC headers or
    the provider can turn a reply draft into a new conversation.
    """

    existing_payload = (
        existing_draft.get("message") if isinstance(existing_draft.get("message"), dict) else {}
    )
    thread_id = str(existing_payload.get("threadId") or "")
    if not thread_id:
        return message_payload

    existing_message = _decode_gmail_raw_message(existing_payload)
    updated_message = _decode_gmail_raw_message(message_payload)
    for header in ("Subject", "In-Reply-To", "References"):
        if header in updated_message:
            del updated_message[header]
        if existing_message.get(header):
            updated_message[header] = str(existing_message[header])

    raw = base64.urlsafe_b64encode(updated_message.as_bytes()).decode("ascii").rstrip("=")
    return {"raw": raw, "threadId": thread_id}


def normalize_gmail_draft(payload: dict[str, Any]) -> EmailDraftSnapshot:
    """Decode Gmail's MIME draft into the provider-neutral review contract."""

    message_payload = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    message = _decode_gmail_raw_message(message_payload)
    to = _draft_addresses(message, "To")
    cc = _draft_addresses(message, "Cc")
    bcc = _draft_addresses(message, "Bcc")
    body, body_content_type = _draft_body(message)
    sender_email = next(iter(_draft_addresses(message, "From")), None)
    subject = str(message.get("Subject") or "")
    draft_id = str(payload.get("id") or "")
    hash_value = email_draft_content_hash(
        sender_email=sender_email.email if sender_email else None,
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        body=body,
        body_content_type=body_content_type,
    )
    return EmailDraftSnapshot(
        draft_id=draft_id,
        sender_email=sender_email.email if sender_email else None,
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        body=body,
        body_content_type=body_content_type,
        content_hash=hash_value,
    )


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


async def _gmail_json_request(
    client: httpx.AsyncClient,
    method: str,
    endpoint: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call one Gmail draft endpoint and keep permission translation consistent."""

    response = await client.request(
        method,
        f"{GMAIL_BASE_URL}{endpoint}",
        json=json_body,
        params=params,
    )
    if _is_google_permission_error(response):
        raise IntegrationPermissionError(
            "Gmail draft permission was not granted. Please reconnect Gmail.",
            status_code=403,
            provider="google",
            endpoint=endpoint,
            response_summary=response.text,
        )
    response.raise_for_status()
    if response.status_code == 204 or not response.content:
        return {}
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


class GmailEmailSendClient:
    """Send new Gmail messages through Gmail users.messages.send."""

    async def send_mail(
        self,
        token_info: IntegrationTokenInfo,
        request: EmailSendCreateRequest,
    ) -> EmailSendProviderResult:
        """Send one new Gmail message after the service resolves mailbox authority."""

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

    async def save_draft(
        self,
        token_info: IntegrationTokenInfo,
        request: EmailDraftSaveRequest,
    ) -> EmailDraftSnapshot:
        """Create or replace a Gmail draft and return its stable draft identity."""

        message_payload = build_gmail_send_message_payload(request)
        endpoint = "/gmail/v1/users/me/drafts"
        method = "POST"
        if request.draft_id:
            endpoint = f"{endpoint}/{request.draft_id}"
            method = "PUT"

        async def _request(access_token: str) -> dict[str, Any]:
            headers = {"Authorization": f"Bearer {access_token}"}
            async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
                provider_message = message_payload
                if request.draft_id:
                    existing = await _gmail_json_request(
                        client,
                        "GET",
                        f"/gmail/v1/users/me/drafts/{request.draft_id}",
                        params={"format": "raw"},
                    )
                    provider_message = preserve_gmail_draft_thread(provider_message, existing)
                saved = await _gmail_json_request(
                    client,
                    method,
                    endpoint,
                    json_body={"message": provider_message},
                )
                # Gmail create/update responses omit raw MIME; read it back so the
                # confirmation hash represents provider state rather than input.
                return await _gmail_json_request(
                    client,
                    "GET",
                    f"/gmail/v1/users/me/drafts/{saved['id']}",
                    params={"format": "raw"},
                )

        try:
            return normalize_gmail_draft(await call_with_token_retry(token_info, _request))
        except IntegrationPermissionError as exc:
            raise EmailSendError(
                "email_draft_scope_required",
                "Reconnect Gmail with permission to manage drafts.",
                status_code=409,
            ) from exc
        except (AuthTokenRevokedError, httpx.HTTPStatusError) as exc:
            raise self._draft_error(exc) from exc

    async def get_draft(
        self,
        token_info: IntegrationTokenInfo,
        draft_id: str,
    ) -> EmailDraftSnapshot:
        """Retrieve the current Gmail draft for approval or conflict detection."""

        async def _request(access_token: str) -> dict[str, Any]:
            headers = {"Authorization": f"Bearer {access_token}"}
            async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
                return await _gmail_json_request(
                    client,
                    "GET",
                    f"/gmail/v1/users/me/drafts/{draft_id}",
                    params={"format": "raw"},
                )

        try:
            return normalize_gmail_draft(await call_with_token_retry(token_info, _request))
        except (IntegrationPermissionError, AuthTokenRevokedError, httpx.HTTPStatusError) as exc:
            raise self._draft_error(exc) from exc

    async def send_draft(
        self,
        token_info: IntegrationTokenInfo,
        draft_id: str,
    ) -> EmailSendProviderResult:
        """Send an existing Gmail draft without reconstructing its content."""

        async def _request(access_token: str) -> dict[str, Any]:
            headers = {"Authorization": f"Bearer {access_token}"}
            async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
                return await _gmail_json_request(
                    client,
                    "POST",
                    "/gmail/v1/users/me/drafts/send",
                    json_body={"id": draft_id},
                )

        try:
            result = await call_with_token_retry(token_info, _request)
        except (IntegrationPermissionError, AuthTokenRevokedError, httpx.HTTPStatusError) as exc:
            raise self._draft_error(exc) from exc
        return EmailSendProviderResult(
            provider_status_code=200,
            provider_resource_id=str(result.get("id")) if result.get("id") else None,
        )

    @staticmethod
    def _draft_error(exc: Exception) -> EmailSendError:
        """Translate Gmail draft failures without exposing provider response bodies."""

        if isinstance(exc, IntegrationPermissionError):
            return EmailSendError(
                "email_draft_scope_required",
                "Reconnect Gmail with permission to manage drafts.",
                status_code=409,
            )
        if isinstance(exc, AuthTokenRevokedError):
            return EmailSendError(
                "email_reauth_required",
                "Reconnect Gmail before managing email drafts.",
                status_code=409,
            )
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
            return EmailSendError(
                "email_draft_not_found",
                "The Gmail draft no longer exists.",
                status_code=404,
            )
        return EmailSendError(
            "email_draft_provider_error",
            "Gmail could not complete the draft operation.",
            status_code=502,
        )
