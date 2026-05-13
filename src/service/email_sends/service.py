"""Production service for creating user-owned mailbox email sends."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import (
    IntegrationActivityRequest,
    IntegrationActivityRequestStatus,
    IntegrationType,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.utils.logger import get_logger
from src.service.email_sends.gmail_client import GmailEmailSendClient
from src.service.email_sends.microsoft_graph_client import MicrosoftEmailSendClient
from src.service.email_sends.models import (
    EmailSendCreateRequest,
    EmailSendCreateResult,
    EmailSendError,
    EmailSendProviderResult,
)
from src.service.email_sends.resolver import EmailIntegrationResolver

logger = get_logger(__name__)

EMAIL_SEND_ACTIVITY_TYPE = "send_email"


class EmailSendClient(Protocol):
    async def send_mail(
        self,
        token_info,
        request: EmailSendCreateRequest,
    ) -> EmailSendProviderResult | None: ...


def _activity_status_value(status: IntegrationActivityRequestStatus | str) -> str:
    return status.value if isinstance(status, IntegrationActivityRequestStatus) else str(status)


def derive_email_idempotency_key(
    request: EmailSendCreateRequest,
    *,
    integration_id: UUID | None = None,
) -> str:
    """Derive a stable key from sender, recipients, and message content."""
    recipient_part = "|".join(
        [
            "to:" + ",".join(sorted(recipient.email for recipient in request.to)),
            "cc:" + ",".join(sorted(recipient.email for recipient in request.cc)),
            "bcc:" + ",".join(sorted(recipient.email for recipient in request.bcc)),
        ]
    )
    raw = "|".join(
        [
            str(request.organisation_id),
            str(request.requested_by_user_id),
            str(integration_id or ""),
            (request.sender_email or "").strip().lower(),
            recipient_part,
            request.subject,
            request.body,
            request.body_content_type,
            str(request.save_to_sent_items),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


class EmailSendService:
    """Send new emails with service-owned security and audit boundaries."""

    def __init__(
        self,
        session,
        *,
        organisation_id: UUID,
        requested_by_user_id: UUID,
        integration_resolver: EmailIntegrationResolver | None = None,
        graph_client: MicrosoftEmailSendClient | None = None,
        provider_clients: dict[IntegrationType, EmailSendClient] | None = None,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.requested_by_user_id = requested_by_user_id
        self.integration_resolver = integration_resolver or EmailIntegrationResolver()
        self.provider_clients: dict[IntegrationType, EmailSendClient] = provider_clients or {
            IntegrationType.OUTLOOK_EMAIL: graph_client or MicrosoftEmailSendClient(),
            IntegrationType.GMAIL: GmailEmailSendClient(),
        }

    async def send_email(self, request: EmailSendCreateRequest) -> EmailSendCreateResult:
        """Create and persist a single email send with status transitions."""
        if request.organisation_id != self.organisation_id:
            raise EmailSendError(
                "organisation_boundary_mismatch",
                "Email send organisation does not match the current request.",
                status_code=403,
            )
        if request.requested_by_user_id != self.requested_by_user_id:
            raise EmailSendError(
                "user_boundary_mismatch",
                "Email send user does not match the current request.",
                status_code=403,
            )

        resolved = await self.integration_resolver.resolve(
            self.session,
            self.organisation_id,
            self.requested_by_user_id,
            sender_email=request.sender_email,
        )
        sender_email = request.sender_email or resolved.integration.integration_email
        if not sender_email:
            raise EmailSendError(
                "email_sender_selection_required",
                "Choose the sender mailbox before I send this email.",
                status_code=409,
            )

        idempotency_key = request.idempotency_key or derive_email_idempotency_key(
            request,
            integration_id=resolved.integration.integration_id,
        )
        existing = await self._find_existing_request(idempotency_key)
        existing_status = _activity_status_value(existing.status) if existing else None
        if existing and not self._existing_matches_request(existing, request, sender_email):
            raise EmailSendError(
                "email_send_idempotency_conflict",
                "This idempotency key was already used for a different email send request.",
                status_code=409,
            )
        if existing and existing_status == IntegrationActivityRequestStatus.SENT.value:
            return EmailSendCreateResult(
                request_id=existing.request_id,
                status="sent",
                subject=str(existing.request_payload.get("subject") or request.subject),
                provider_status_code=existing.provider_status_code,
                linked_record_type=existing.linked_record_type,
                linked_record_id=existing.linked_record_id,
                provider_resource_id=existing.provider_resource_id,
            )
        if existing and existing_status == IntegrationActivityRequestStatus.SENDING.value:
            raise EmailSendError(
                "email_send_status_unknown",
                (
                    "This email send is already in progress or its final provider status is "
                    "unknown. Confirm before attempting to resend."
                ),
                status_code=409,
            )
        if existing:
            raise EmailSendError(
                "email_send_replay_blocked",
                (
                    "A previous attempt for this email already exists and was not confirmed "
                    "as sent. Confirm before attempting to resend."
                ),
                status_code=409,
            )

        await self._ensure_deal_access(request.deal_id)
        audit_row = self._build_audit_row(
            request,
            integration_id=resolved.integration.integration_id,
            sender_email=sender_email,
            idempotency_key=idempotency_key,
        )
        self.session.add(audit_row)
        await self.session.flush()

        try:
            audit_row.status = IntegrationActivityRequestStatus.SENDING
            audit_row.history = self._append_history(audit_row, "SENDING")
            audit_row.updated_at = datetime.now(UTC)
            await self.session.flush()
            await self.session.commit()

            send_request = request.model_copy(
                update={"sender_email": sender_email, "idempotency_key": idempotency_key}
            )
            send_client = self._get_provider_client(resolved.integration.integration_type)
            provider_result = await send_client.send_mail(resolved.token_info, send_request)
            provider_status_code = provider_result.provider_status_code if provider_result else 202
            provider_resource_id = provider_result.provider_resource_id if provider_result else None

            audit_row.status = IntegrationActivityRequestStatus.SENT
            audit_row.provider_status_code = provider_status_code
            audit_row.provider_resource_id = provider_resource_id
            audit_row.history = self._append_history(audit_row, "SENT")
            audit_row.updated_at = datetime.now(UTC)
            await self.session.commit()

            return EmailSendCreateResult(
                request_id=audit_row.request_id,
                status="sent",
                subject=str(audit_row.request_payload.get("subject") or request.subject),
                provider_status_code=audit_row.provider_status_code,
                linked_record_type=audit_row.linked_record_type,
                linked_record_id=audit_row.linked_record_id,
                provider_resource_id=audit_row.provider_resource_id,
            )
        except EmailSendError as exc:
            await self._mark_failed(audit_row, exc)
            raise
        except Exception as exc:
            logger.exception("Email send failed")
            wrapped = EmailSendError(
                "email_send_failed",
                "Could not send the email.",
                status_code=502,
            )
            await self._mark_failed(audit_row, wrapped)
            raise wrapped from exc

    def _get_provider_client(self, integration_type: IntegrationType) -> EmailSendClient:
        client = self.provider_clients.get(integration_type)
        if client is None:
            raise EmailSendError(
                "email_send_provider_unsupported",
                "This mailbox provider cannot send emails yet.",
                status_code=409,
            )
        return client

    def _build_audit_row(
        self,
        request: EmailSendCreateRequest,
        *,
        integration_id: UUID,
        sender_email: str,
        idempotency_key: str,
    ) -> IntegrationActivityRequest:
        participants = (
            [{"kind": "to", **recipient.model_dump()} for recipient in request.to]
            + [{"kind": "cc", **recipient.model_dump()} for recipient in request.cc]
            + [{"kind": "bcc", **recipient.model_dump()} for recipient in request.bcc]
        )
        return IntegrationActivityRequest(
            organisation_id=self.organisation_id,
            requested_by_user_id=self.requested_by_user_id,
            integration_id=integration_id,
            deal_id=request.deal_id,
            activity_type=EMAIL_SEND_ACTIVITY_TYPE,
            idempotency_key=idempotency_key,
            title=request.subject,
            body=request.body,
            participants=participants,
            request_payload=self._request_payload(request, sender_email),
            status=IntegrationActivityRequestStatus.PENDING,
            history=[self._history_entry("PENDING")],
        )

    @staticmethod
    def _request_payload(request: EmailSendCreateRequest, sender_email: str) -> dict:
        return {
            "sender_email": sender_email,
            "to": [recipient.model_dump() for recipient in request.to],
            "cc": [recipient.model_dump() for recipient in request.cc],
            "bcc": [recipient.model_dump() for recipient in request.bcc],
            "subject": request.subject,
            "body": request.body,
            "body_content_type": request.body_content_type,
            "save_to_sent_items": request.save_to_sent_items,
        }

    def _existing_matches_request(
        self,
        existing: IntegrationActivityRequest,
        request: EmailSendCreateRequest,
        sender_email: str,
    ) -> bool:
        return dict(existing.request_payload or {}) == self._request_payload(request, sender_email)

    async def _find_existing_request(
        self, idempotency_key: str
    ) -> IntegrationActivityRequest | None:
        stmt = select(IntegrationActivityRequest).where(
            col(IntegrationActivityRequest.organisation_id) == self.organisation_id,
            col(IntegrationActivityRequest.requested_by_user_id) == self.requested_by_user_id,
            col(IntegrationActivityRequest.activity_type) == EMAIL_SEND_ACTIVITY_TYPE,
            col(IntegrationActivityRequest.idempotency_key) == idempotency_key,
        )
        result = await self.session.execute(stmt)
        return cast(IntegrationActivityRequest | None, result.scalars().first())

    async def _ensure_deal_access(self, deal_id: UUID | None) -> None:
        if deal_id is None:
            return
        stmt = select(CRMDeal).where(
            col(CRMDeal.organisation_id) == self.organisation_id,
            col(CRMDeal.deal_id) == deal_id,
            col(CRMDeal.is_deleted) == False,  # noqa: E712
        )
        result = await self.session.execute(stmt)
        if result.scalar_one_or_none() is None:
            raise EmailSendError(
                "deal_not_found",
                "The requested deal does not exist in this organisation.",
                status_code=404,
            )

    async def _mark_failed(
        self,
        audit_row: IntegrationActivityRequest,
        exc: EmailSendError,
    ) -> None:
        audit_row.status = IntegrationActivityRequestStatus.FAILED
        audit_row.error_code = exc.code
        audit_row.error_message = exc.message
        audit_row.history = self._append_history(audit_row, "FAILED", error_code=exc.code)
        audit_row.updated_at = datetime.now(UTC)
        try:
            await self.session.commit()
        except Exception:
            logger.exception("Failed to persist email send failure state")
            await self.session.rollback()

    @staticmethod
    def _history_entry(status: str, *, error_code: str | None = None) -> dict[str, str]:
        entry = {"status": status, "at": datetime.now(UTC).isoformat()}
        if error_code:
            entry["error_code"] = error_code
        return entry

    def _append_history(
        self,
        audit_row: IntegrationActivityRequest,
        status: str,
        *,
        error_code: str | None = None,
    ) -> list[dict]:
        return [*list(audit_row.history or []), self._history_entry(status, error_code=error_code)]
