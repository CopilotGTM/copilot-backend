"""Production service for creating user-owned mailbox email sends."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import (
    Email,
    IntegrationActivityRequest,
    IntegrationActivityRequestStatus,
    IntegrationType,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.config.google_integration_scopes import GOOGLE_SCOPE_GMAIL_COMPOSE
from src.infra.config.microsoft_integration_scopes import MICROSOFT_SCOPE_MAIL_READ_WRITE
from src.infra.utils.logger import get_logger
from src.service.email_sends.gmail_client import GmailEmailSendClient
from src.service.email_sends.microsoft_graph_client import MicrosoftEmailSendClient
from src.service.email_sends.models import (
    EmailDraftOperationResult,
    EmailDraftSaveRequest,
    EmailDraftSnapshot,
    EmailSendAddress,
    EmailSendCreateRequest,
    EmailSendCreateResult,
    EmailSendError,
    EmailSendProviderResult,
)
from src.service.email_sends.resolver import EmailIntegrationResolver
from src.service.integrations.scope_validation import (
    get_scope_display_label,
    is_required_scope_satisfied,
)

logger = get_logger(__name__)

EMAIL_SEND_ACTIVITY_TYPE = "send_email"
EMAIL_DRAFT_SAVE_ACTIVITY_TYPE = "save_email_draft"
EMAIL_DRAFT_UPDATE_ACTIVITY_TYPE = "update_email_draft"
EMAIL_DRAFT_SEND_ACTIVITY_TYPE = "send_email_draft"


class EmailSendClient(Protocol):
    """Provider operations required by the mailbox send and draft service."""

    async def send_mail(
        self,
        token_info,
        request: EmailSendCreateRequest,
    ) -> EmailSendProviderResult | None:
        """Send one new message through the resolved mailbox provider."""
        ...

    async def save_draft(
        self,
        token_info,
        request: EmailDraftSaveRequest,
    ) -> EmailDraftSnapshot:
        """Create or update one new or threaded provider draft."""
        ...

    async def get_draft(self, token_info, draft_id: str) -> EmailDraftSnapshot:
        """Return the provider's current draft state for review."""
        ...

    async def send_draft(
        self,
        token_info,
        draft_id: str,
    ) -> EmailSendProviderResult:
        """Send one provider draft that the service has already reviewed."""
        ...


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
    """Manage user-owned drafts and sends behind org and mailbox boundaries."""

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
            IntegrationType.MICROSOFT: graph_client or MicrosoftEmailSendClient(),
            IntegrationType.GOOGLE: GmailEmailSendClient(),
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

    async def save_draft(self, request: EmailDraftSaveRequest) -> EmailDraftOperationResult:
        """Create or replace one provider draft and persist an auditable operation."""

        self._validate_request_boundary(request)
        resolved = await self.integration_resolver.resolve(
            self.session,
            self.organisation_id,
            self.requested_by_user_id,
            sender_email=request.sender_email,
        )
        self._require_draft_scope(resolved.integration)
        sender_email = request.sender_email or resolved.integration.integration_email
        if not sender_email:
            raise EmailSendError(
                "email_sender_selection_required",
                "Choose the sender mailbox before saving this draft.",
                status_code=409,
            )
        provider_request = await self._prepare_reply_draft(
            request.model_copy(update={"sender_email": sender_email}),
            integration_id=resolved.integration.integration_id,
            sender_email=sender_email,
        )
        await self._ensure_deal_access(request.deal_id)
        operation = (
            EMAIL_DRAFT_UPDATE_ACTIVITY_TYPE if request.draft_id else EMAIL_DRAFT_SAVE_ACTIVITY_TYPE
        )
        idempotency_key = request.idempotency_key or self._draft_operation_key(
            operation,
            provider_request,
            resolved.integration.integration_id,
        )
        existing = await self._find_activity_request(operation, idempotency_key)
        client = self._get_provider_client(resolved.integration.integration_type)
        if (
            existing
            and _activity_status_value(existing.status)
            == IntegrationActivityRequestStatus.SENT.value
        ):
            draft_id = existing.provider_resource_id
            if not draft_id:
                raise EmailSendError(
                    "email_draft_status_unknown",
                    "The draft operation completed without a provider draft ID.",
                    status_code=409,
                )
            snapshot = await client.get_draft(resolved.token_info, draft_id)
            return EmailDraftOperationResult(
                request_id=existing.request_id,
                status="draft_saved",
                snapshot=snapshot,
                provider_status_code=existing.provider_status_code,
            )
        if existing:
            raise EmailSendError(
                "email_draft_operation_in_progress",
                "This draft operation is already in progress or failed with an unknown result.",
                status_code=409,
            )

        audit_row = self._build_draft_audit_row(
            provider_request,
            operation=operation,
            integration_id=resolved.integration.integration_id,
            idempotency_key=idempotency_key,
        )
        self.session.add(audit_row)
        await self.session.flush()
        try:
            # Commit intent before the external write so a timeout cannot be
            # retried as a new provider draft without an operator-visible trace.
            await self._mark_sending(audit_row)
            snapshot = await client.save_draft(resolved.token_info, provider_request)
            audit_row.status = IntegrationActivityRequestStatus.SENT
            audit_row.provider_resource_id = snapshot.draft_id
            audit_row.provider_status_code = 200
            audit_row.history = self._append_history(audit_row, "SENT")
            audit_row.updated_at = datetime.now(UTC)
            await self.session.commit()
            return EmailDraftOperationResult(
                request_id=audit_row.request_id,
                status="draft_saved",
                snapshot=snapshot,
                provider_status_code=audit_row.provider_status_code,
            )
        except EmailSendError as exc:
            await self._mark_failed(audit_row, exc)
            raise
        except Exception as exc:
            wrapped = EmailSendError(
                "email_draft_save_failed",
                "Could not save the email draft.",
                status_code=502,
            )
            await self._mark_failed(audit_row, wrapped)
            raise wrapped from exc

    async def _prepare_reply_draft(
        self,
        request: EmailDraftSaveRequest,
        *,
        integration_id: UUID,
        sender_email: str,
    ) -> EmailDraftSaveRequest:
        """Resolve a public message ID to one mailbox-owned provider reply target.

        New drafts and updates pass through unchanged. Reply drafts replace
        model-supplied routing fields with synchronized source data, so a model
        cannot redirect a reply while still claiming it belongs to that thread.
        """

        source_message_id = request.reply_to_internet_message_id
        if not source_message_id:
            return request

        email_c = cast(Any, Email).__table__.c
        # Organisation and integration predicates are the trust boundary. An
        # RFC Message-ID is shared across recipients and is not authorization.
        statement = (
            select(Email)
            .where(
                email_c.organisation_id == self.organisation_id,
                email_c.integration_id == integration_id,
                email_c.internet_message_id == source_message_id,
                email_c.is_deleted.is_(False),
            )
            .order_by(email_c.received_at.desc(), email_c.email_id.desc())
            .limit(1)
        )
        result = await self.session.execute(statement)
        source = result.scalars().first()
        if source is None:
            raise EmailSendError(
                "email_reply_source_not_found",
                "I could not find that source email in the selected mailbox.",
                status_code=404,
            )
        if not source.external_message_id or not source.thread_id or not source.internet_message_id:
            raise EmailSendError(
                "email_reply_source_incomplete",
                "That email is missing provider thread information required for a reply draft.",
                status_code=409,
            )
        if not source.subject:
            raise EmailSendError(
                "email_reply_subject_missing",
                "That email is missing the subject required to keep the reply in its thread.",
                status_code=409,
            )

        to, cc = self._reply_recipients(source, sender_email=sender_email)
        references = list(
            dict.fromkeys([*list(source.references or []), source.internet_message_id])
        )
        subject = (
            source.subject if source.subject.lower().startswith("re:") else f"Re: {source.subject}"
        )
        return cast(
            EmailDraftSaveRequest,
            request.model_copy(
                update={
                    "to": to,
                    "cc": cc,
                    "bcc": [],
                    "subject": subject,
                    "reply_to_external_message_id": source.external_message_id,
                    "reply_thread_id": source.thread_id,
                    "reply_references": references,
                    "reply_mode": "reply_all",
                }
            ),
        )

    @staticmethod
    def _reply_recipients(
        source: Email,
        *,
        sender_email: str,
    ) -> tuple[list[EmailSendAddress], list[EmailSendAddress]]:
        """Preserve source-message participants without addressing the mailbox owner."""

        sender = sender_email.strip().lower()
        source_from = (source.from_email or "").strip().lower()
        if source_from and source_from != sender:
            primary = [(source_from, source.from_name)]
        else:
            # A follow-up anchored to the user's own last message should go back
            # to its original recipients instead of creating a reply to self.
            primary = [(address, None) for address in list(source.to_emails or [])]

        seen = {sender}

        def _addresses(values: list[tuple[str, str | None]]) -> list[EmailSendAddress]:
            resolved: list[EmailSendAddress] = []
            for raw_email, name in values:
                normalized = (raw_email or "").strip().lower()
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                resolved.append(EmailSendAddress(email=normalized, name=name))
            return resolved

        to = _addresses(primary)
        cc_candidates = [(address, None) for address in list(source.to_emails or [])]
        cc_candidates.extend((address, None) for address in list(source.cc_emails or []))
        cc = _addresses(cc_candidates)
        if not to:
            raise EmailSendError(
                "email_reply_recipients_missing",
                "That email does not contain a valid recipient for this reply.",
                status_code=409,
            )
        return to, cc

    async def get_draft(
        self,
        *,
        draft_id: str,
        sender_email: str | None,
    ) -> EmailDraftSnapshot:
        """Read one draft only after resolving its current-user-owned mailbox."""

        resolved = await self.integration_resolver.resolve(
            self.session,
            self.organisation_id,
            self.requested_by_user_id,
            sender_email=sender_email,
        )
        self._require_draft_scope(resolved.integration)
        return await self._get_provider_client(resolved.integration.integration_type).get_draft(
            resolved.token_info, draft_id
        )

    async def send_draft(
        self,
        *,
        draft_id: str,
        sender_email: str | None,
        expected_content_hash: str,
        idempotency_key: str | None = None,
    ) -> EmailDraftOperationResult:
        """Send one unchanged provider draft after exact-content confirmation."""

        resolved = await self.integration_resolver.resolve(
            self.session,
            self.organisation_id,
            self.requested_by_user_id,
            sender_email=sender_email,
        )
        self._require_draft_scope(resolved.integration)
        client = self._get_provider_client(resolved.integration.integration_type)
        snapshot = await client.get_draft(resolved.token_info, draft_id)
        if snapshot.content_hash != expected_content_hash:
            raise EmailSendError(
                "email_draft_changed",
                "The draft changed after it was reviewed. Please review the latest version.",
                status_code=409,
            )
        operation_key = (
            idempotency_key
            or hashlib.sha256(
                f"{resolved.integration.integration_id}|{draft_id}|{expected_content_hash}".encode()
            ).hexdigest()[:64]
        )
        existing = await self._find_activity_request(
            EMAIL_DRAFT_SEND_ACTIVITY_TYPE,
            operation_key,
        )
        if (
            existing
            and _activity_status_value(existing.status)
            == IntegrationActivityRequestStatus.SENT.value
        ):
            return EmailDraftOperationResult(
                request_id=existing.request_id,
                status="sent",
                snapshot=snapshot,
                provider_status_code=existing.provider_status_code,
            )
        if existing:
            raise EmailSendError(
                "email_draft_send_status_unknown",
                "This draft send is already in progress or its result is unknown.",
                status_code=409,
            )

        audit_row = self._build_snapshot_audit_row(
            snapshot,
            integration_id=resolved.integration.integration_id,
            idempotency_key=operation_key,
        )
        self.session.add(audit_row)
        await self.session.flush()
        try:
            await self._mark_sending(audit_row)
            provider_result = await client.send_draft(resolved.token_info, draft_id)
            audit_row.status = IntegrationActivityRequestStatus.SENT
            audit_row.provider_status_code = provider_result.provider_status_code
            audit_row.provider_resource_id = provider_result.provider_resource_id or draft_id
            audit_row.history = self._append_history(audit_row, "SENT")
            audit_row.updated_at = datetime.now(UTC)
            await self.session.commit()
            return EmailDraftOperationResult(
                request_id=audit_row.request_id,
                status="sent",
                snapshot=snapshot,
                provider_status_code=audit_row.provider_status_code,
            )
        except EmailSendError as exc:
            await self._mark_failed(audit_row, exc)
            raise
        except Exception as exc:
            wrapped = EmailSendError(
                "email_draft_send_failed",
                "Could not send the email draft.",
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

    @staticmethod
    def _require_draft_scope(integration) -> None:
        required_scope = (
            GOOGLE_SCOPE_GMAIL_COMPOSE
            if integration.integration_type == IntegrationType.GOOGLE
            else MICROSOFT_SCOPE_MAIL_READ_WRITE
        )
        if is_required_scope_satisfied(required_scope, list(integration.scopes or [])):
            return
        label = get_scope_display_label(required_scope)
        raise EmailSendError(
            "email_draft_scope_required",
            f"Reconnect this mailbox with {label} before using email drafts.",
            status_code=409,
        )

    def _validate_request_boundary(self, request: EmailSendCreateRequest) -> None:
        """Reject replayed payloads that do not match the immutable service context."""

        if request.organisation_id != self.organisation_id:
            raise EmailSendError(
                "organisation_boundary_mismatch",
                "Email operation organisation does not match the current request.",
                status_code=403,
            )
        if request.requested_by_user_id != self.requested_by_user_id:
            raise EmailSendError(
                "user_boundary_mismatch",
                "Email operation user does not match the current request.",
                status_code=403,
            )

    @staticmethod
    def _draft_operation_key(
        operation: str,
        request: EmailDraftSaveRequest,
        integration_id: UUID,
    ) -> str:
        raw = f"{operation}|{integration_id}|{request.model_dump_json(exclude={'idempotency_key'})}"
        return hashlib.sha256(raw.encode()).hexdigest()[:64]

    def _build_draft_audit_row(
        self,
        request: EmailDraftSaveRequest,
        *,
        operation: str,
        integration_id: UUID,
        idempotency_key: str,
    ) -> IntegrationActivityRequest:
        row = self._build_audit_row(
            request,
            integration_id=integration_id,
            sender_email=request.sender_email or "",
            idempotency_key=idempotency_key,
        )
        row.activity_type = operation
        row.request_payload["draft_id"] = request.draft_id
        if request.reply_to_internet_message_id:
            row.request_payload["reply_to_internet_message_id"] = (
                request.reply_to_internet_message_id
            )
            row.request_payload["reply_mode"] = request.reply_mode
        return row

    def _build_snapshot_audit_row(
        self,
        snapshot: EmailDraftSnapshot,
        *,
        integration_id: UUID,
        idempotency_key: str,
    ) -> IntegrationActivityRequest:
        return IntegrationActivityRequest(
            organisation_id=self.organisation_id,
            requested_by_user_id=self.requested_by_user_id,
            integration_id=integration_id,
            activity_type=EMAIL_DRAFT_SEND_ACTIVITY_TYPE,
            idempotency_key=idempotency_key,
            title=snapshot.subject,
            body=snapshot.body,
            participants=(
                [{"kind": "to", **item.model_dump()} for item in snapshot.to]
                + [{"kind": "cc", **item.model_dump()} for item in snapshot.cc]
                + [{"kind": "bcc", **item.model_dump()} for item in snapshot.bcc]
            ),
            provider_resource_id=snapshot.draft_id,
            request_payload={
                "draft_id": snapshot.draft_id,
                "sender_email": snapshot.sender_email,
                "content_hash": snapshot.content_hash,
            },
            status=IntegrationActivityRequestStatus.PENDING,
            history=[self._history_entry("PENDING")],
        )

    async def _mark_sending(self, audit_row: IntegrationActivityRequest) -> None:
        """Durably mark provider intent before crossing the external side-effect boundary."""

        audit_row.status = IntegrationActivityRequestStatus.SENDING
        audit_row.history = self._append_history(audit_row, "SENDING")
        audit_row.updated_at = datetime.now(UTC)
        await self.session.commit()

    async def _find_activity_request(
        self,
        activity_type: str,
        idempotency_key: str,
    ) -> IntegrationActivityRequest | None:
        # The org/user predicates are the trust boundary; an idempotency key can
        # be supplied by an agent and is never globally trusted by itself.
        stmt = select(IntegrationActivityRequest).where(
            col(IntegrationActivityRequest.organisation_id) == self.organisation_id,
            col(IntegrationActivityRequest.requested_by_user_id) == self.requested_by_user_id,
            col(IntegrationActivityRequest.activity_type) == activity_type,
            col(IntegrationActivityRequest.idempotency_key) == idempotency_key,
        )
        result = await self.session.execute(stmt)
        return cast(IntegrationActivityRequest | None, result.scalars().first())

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
