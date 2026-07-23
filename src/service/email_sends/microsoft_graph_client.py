"""Microsoft Graph client helpers for Outlook sendMail."""

from __future__ import annotations

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
from src.workers.integrations_worker.providers._microsoft_graph import (
    graph_api_patch_json,
    graph_api_post_json,
    graph_api_post_no_content,
    graph_api_request,
)


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


def build_microsoft_draft_payload(request: EmailDraftSaveRequest) -> dict[str, Any]:
    """Build the editable Graph message fields shared by create and update."""

    payload = build_microsoft_send_mail_payload(request)["message"]
    return payload


def _normalize_graph_recipients(values: Any) -> list[EmailSendAddress]:
    recipients: list[EmailSendAddress] = []
    for value in values if isinstance(values, list) else []:
        address = value.get("emailAddress") if isinstance(value, dict) else None
        if not isinstance(address, dict) or not address.get("address"):
            continue
        recipients.append(
            EmailSendAddress(
                email=str(address["address"]),
                name=str(address["name"]) if address.get("name") else None,
            )
        )
    return recipients


def normalize_microsoft_draft(payload: dict[str, Any]) -> EmailDraftSnapshot:
    """Normalize a Graph message draft into the exact review payload."""

    body_payload = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    sender_payload = payload.get("from") if isinstance(payload.get("from"), dict) else {}
    sender_address = (
        sender_payload.get("emailAddress")
        if isinstance(sender_payload.get("emailAddress"), dict)
        else {}
    )
    to = _normalize_graph_recipients(payload.get("toRecipients"))
    cc = _normalize_graph_recipients(payload.get("ccRecipients"))
    bcc = _normalize_graph_recipients(payload.get("bccRecipients"))
    sender_email = str(sender_address.get("address")) if sender_address.get("address") else None
    subject = str(payload.get("subject") or "")
    body = str(body_payload.get("content") or "")
    body_content_type = "HTML" if str(body_payload.get("contentType")).lower() == "html" else "Text"
    return EmailDraftSnapshot(
        draft_id=str(payload.get("id") or ""),
        sender_email=sender_email,
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        body=body,
        body_content_type=body_content_type,
        content_hash=email_draft_content_hash(
            sender_email=sender_email,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=body,
            body_content_type=body_content_type,
        ),
        web_link=str(payload["webLink"]) if payload.get("webLink") else None,
    )


class MicrosoftEmailSendClient:
    """Send new Outlook messages through Microsoft Graph sendMail."""

    async def send_mail(
        self,
        token_info: IntegrationTokenInfo,
        request: EmailSendCreateRequest,
    ) -> EmailSendProviderResult:
        """Send one new Outlook message after the service resolves mailbox authority."""

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
            detail = extract_api_error_details(exc).summary
            suffix = f" Provider said: {detail}" if detail else ""
            raise EmailSendError(
                "email_send_provider_error",
                f"Outlook Email rejected the send request.{suffix}",
                status_code=502,
            ) from exc
        return EmailSendProviderResult(provider_status_code=202)

    async def save_draft(
        self,
        token_info: IntegrationTokenInfo,
        request: EmailDraftSaveRequest,
    ) -> EmailDraftSnapshot:
        """Create a new/reply draft or patch one Outlook draft in the current mailbox."""

        payload = build_microsoft_draft_payload(request)

        async def _request(access_token: str) -> dict[str, Any]:
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
                if request.reply_to_external_message_id:
                    # Graph owns reply recipient and conversation semantics. Use
                    # its native operation instead of imitating a reply with a
                    # newly-created message that only has a matching subject.
                    action = (
                        "createReplyAll" if request.reply_mode == "reply_all" else "createReply"
                    )
                    return await graph_api_post_json(
                        client,
                        f"/v1.0/me/messages/{request.reply_to_external_message_id}/{action}",
                        json_body={"message": {"body": payload["body"]}},
                    )
                if request.draft_id:
                    return await graph_api_patch_json(
                        client,
                        f"/v1.0/me/messages/{request.draft_id}",
                        json_body=payload,
                    )
                return await graph_api_post_json(client, "/v1.0/me/messages", json_body=payload)

        try:
            return normalize_microsoft_draft(await call_with_token_retry(token_info, _request))
        except (IntegrationPermissionError, AuthTokenRevokedError, httpx.HTTPStatusError) as exc:
            raise self._draft_error(exc) from exc

    async def get_draft(
        self,
        token_info: IntegrationTokenInfo,
        draft_id: str,
    ) -> EmailDraftSnapshot:
        """Retrieve an Outlook draft and reject resources that have already been sent."""

        async def _request(access_token: str) -> dict[str, Any]:
            headers = {"Authorization": f"Bearer {access_token}"}
            async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
                return await graph_api_request(client, f"/v1.0/me/messages/{draft_id}")

        try:
            payload = await call_with_token_retry(token_info, _request)
        except (IntegrationPermissionError, AuthTokenRevokedError, httpx.HTTPStatusError) as exc:
            raise self._draft_error(exc) from exc
        if payload.get("isDraft") is not True:
            raise EmailSendError(
                "email_draft_already_sent",
                "This Outlook message is no longer a draft.",
                status_code=409,
            )
        return normalize_microsoft_draft(payload)

    async def send_draft(
        self,
        token_info: IntegrationTokenInfo,
        draft_id: str,
    ) -> EmailSendProviderResult:
        """Send a previously reviewed Outlook draft."""

        async def _request(access_token: str) -> None:
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
                await graph_api_post_no_content(
                    client,
                    f"/v1.0/me/messages/{draft_id}/send",
                    json_body={},
                )

        try:
            await call_with_token_retry(token_info, _request)
        except (IntegrationPermissionError, AuthTokenRevokedError, httpx.HTTPStatusError) as exc:
            raise self._draft_error(exc) from exc
        return EmailSendProviderResult(provider_status_code=202)

    @staticmethod
    def _draft_error(exc: Exception) -> EmailSendError:
        """Translate Graph draft failures into stable agent-facing codes."""

        if isinstance(exc, IntegrationPermissionError):
            return EmailSendError(
                "email_draft_scope_required",
                "Reconnect Outlook with permission to manage drafts.",
                status_code=409,
            )
        if isinstance(exc, AuthTokenRevokedError):
            return EmailSendError(
                "email_reauth_required",
                "Reconnect Outlook before managing email drafts.",
                status_code=409,
            )
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
            return EmailSendError(
                "email_draft_not_found",
                "The Outlook draft no longer exists.",
                status_code=404,
            )
        return EmailSendError(
            "email_draft_provider_error",
            "Outlook could not complete the draft operation.",
            status_code=502,
        )
