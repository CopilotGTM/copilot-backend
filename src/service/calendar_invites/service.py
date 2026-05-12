"""Production service for creating user-owned calendar invites."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import (
    CalendarInviteRequest,
    CalendarInviteRequestStatus,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.utils.logger import get_logger
from src.service.calendar_invites.microsoft_graph_client import MicrosoftCalendarInviteClient
from src.service.calendar_invites.models import (
    CalendarInviteCreateRequest,
    CalendarInviteCreateResult,
    CalendarInviteError,
)
from src.service.calendar_invites.resolver import OutlookCalendarIntegrationResolver
from src.service.calendar_invites.timezones import to_outlook_timezone
from src.service.integrations.calendar.normalizer import CalendarEventNormalizer
from src.service.integrations.calendar.writer import CalendarSyncWriter

logger = get_logger(__name__)


def derive_idempotency_key(request: CalendarInviteCreateRequest) -> str:
    """Derive a stable Graph transactionId from the user intent."""
    # Build a deterministic key from org/user identity, title, window, and attendees.
    # This avoids duplicate Graph writes when the user retries the same action.
    attendee_part = ",".join(sorted(attendee.email for attendee in request.attendees))
    sender_part = (request.sender_email or "").strip().lower()
    raw = "|".join(
        [
            str(request.organisation_id),
            str(request.requested_by_user_id),
            sender_part,
            request.title,
            request.start_at.isoformat(),
            request.end_at.isoformat(),
            attendee_part,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


class CalendarInviteService:
    """Create Outlook Calendar events that send Teams meeting invites.

    The caller supplies an org-scoped session. The service creates the provider
    event and immediately mirrors the provider payload into the canonical
    calendar_events tables through the existing calendar sync writer.
    """

    def __init__(
        self,
        session,
        *,
        organisation_id: UUID,
        requested_by_user_id: UUID,
        integration_resolver: OutlookCalendarIntegrationResolver | None = None,
        graph_client: MicrosoftCalendarInviteClient | None = None,
        normalizer: CalendarEventNormalizer | None = None,
        writer_factory: Any = CalendarSyncWriter,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.requested_by_user_id = requested_by_user_id
        self.integration_resolver = integration_resolver or OutlookCalendarIntegrationResolver()
        self.graph_client = graph_client or MicrosoftCalendarInviteClient()
        self.normalizer = normalizer or CalendarEventNormalizer()
        self.writer_factory = writer_factory

    async def create_invite(
        self,
        request: CalendarInviteCreateRequest,
    ) -> CalendarInviteCreateResult:
        """Create and persist a single calendar invite with status and audit updates."""
        # The service owns idempotency, tenant boundary checks, and write ordering.
        # Keep this as one transaction boundary so partial failures never leak
        # mixed SENT/FAILED states visible to users.
        # Reject cross-org and cross-user payloads before any DB/Graph action.
        # These checks are defensive because tool payloads can be replayed.
        if request.organisation_id != self.organisation_id:
            raise CalendarInviteError(
                "organisation_boundary_mismatch",
                "Calendar invite organisation does not match the current request.",
                status_code=403,
            )
        if request.requested_by_user_id != self.requested_by_user_id:
            raise CalendarInviteError(
                "user_boundary_mismatch",
                "Calendar invite user does not match the current request.",
                status_code=403,
            )

        idempotency_key = request.idempotency_key or derive_idempotency_key(request)
        # Reuse prior successful attempts by idempotency key so chat retry does
        # not send duplicate invites in rapid retry loops.
        existing = await self._find_existing_request(idempotency_key)
        if existing and existing.status == CalendarInviteRequestStatus.SENT:
            return CalendarInviteCreateResult(
                request_id=existing.request_id,
                event_id=existing.event_id,
                provider_event_id=existing.provider_event_id or "",
                subject=existing.title,
                conference_link=existing.conference_link,
            )

        resolved = await self.integration_resolver.resolve(
            self.session,
            self.organisation_id,
            self.requested_by_user_id,
            sender_email=request.sender_email,
        )
        await self._ensure_deal_access(request.deal_id)
        provider_timezone = to_outlook_timezone(request.timezone)
        # Persist audit row before write to preserve operator-visible failure state
        # even when Graph create_event fails mid-flight.
        audit_row = self._build_audit_row(request, resolved.integration.integration_id)
        audit_row.idempotency_key = idempotency_key
        audit_row.provider_timezone = provider_timezone

        self.session.add(audit_row)
        await self.session.flush()

        try:
            # Track ordering explicitly: SENDING -> Graph -> normalized sync -> SENT.
            # Commit SENDING before Graph so a crash after Microsoft accepts the
            # invite still leaves a local repair/audit record keyed by transactionId.
            audit_row.status = CalendarInviteRequestStatus.SENDING
            audit_row.updated_at = datetime.now(UTC)
            await self.session.flush()
            await self.session.commit()

            provider_response = await self.graph_client.create_event(
                resolved.token_info,
                request.model_copy(update={"idempotency_key": idempotency_key}),
            )
            provider_event_id = provider_response.get("id")
            if not isinstance(provider_event_id, str) or not provider_event_id:
                raise CalendarInviteError(
                    "calendar_provider_missing_event_id",
                    "Outlook created no usable calendar event ID.",
                    status_code=502,
                )

            normalized = self.normalizer.normalize(provider_response, "microsoft")
            if not normalized.identity.external_id:
                raise CalendarInviteError(
                    "calendar_provider_missing_ical_uid",
                    "Outlook created the invite but returned no iCalUId for local sync.",
                    status_code=502,
                )

            writer = self.writer_factory(self.session, self.organisation_id)
            # Upsert first before attendees so derived attendee sync can attach to
            # a stable event id and avoid orphan attendee rows.
            event = await writer.upsert_event(resolved.integration, normalized)
            if normalized.user_external_id:
                await writer.upsert_user_state(
                    event,
                    resolved.integration,
                    normalized.user_external_id,
                )
            await writer.sync_attendees(event, normalized)

            audit_row.status = CalendarInviteRequestStatus.SENT
            audit_row.provider_event_id = provider_event_id
            audit_row.provider_response = provider_response
            audit_row.event_id = event.event_id
            audit_row.conference_link = provider_response.get("onlineMeeting", {}).get("joinUrl")
            audit_row.updated_at = datetime.now(UTC)
            await self.session.commit()

            return CalendarInviteCreateResult(
                request_id=audit_row.request_id,
                event_id=event.event_id,
                provider_event_id=provider_event_id,
                subject=provider_response.get("subject") or request.title,
                conference_link=audit_row.conference_link,
                web_link=provider_response.get("webLink"),
            )
        except CalendarInviteError as exc:
            await self._mark_failed(audit_row, exc)
            raise
        except Exception as exc:
            logger.exception("Calendar invite creation failed")
            await self._mark_failed(
                audit_row,
                CalendarInviteError(
                    "calendar_invite_failed",
                    "Could not create the calendar invite.",
                    status_code=502,
                ),
            )
            raise CalendarInviteError(
                "calendar_invite_failed",
                "Could not create the calendar invite.",
                status_code=502,
            ) from exc

    def _build_audit_row(
        self,
        request: CalendarInviteCreateRequest,
        integration_id: UUID,
    ) -> CalendarInviteRequest:
        # Keep an explicit provider_timezone column for parity with Graph payload
        # and to support future troubleshooting when timezone conversions drift.
        return CalendarInviteRequest(
            organisation_id=self.organisation_id,
            requested_by_user_id=self.requested_by_user_id,
            integration_id=integration_id,
            deal_id=request.deal_id,
            title=request.title,
            body=request.body,
            location=request.location,
            start_at=request.start_at,
            end_at=request.end_at,
            timezone=request.timezone,
            provider_timezone=request.timezone,
            attendees=[attendee.model_dump() for attendee in request.attendees],
            idempotency_key=request.idempotency_key or "",
            status=CalendarInviteRequestStatus.PENDING,
        )

    async def _find_existing_request(self, idempotency_key: str) -> CalendarInviteRequest | None:
        # Query by org/user/idempotency so replay from one user cannot observe
        # or collide with another user's previous attempt.
        stmt = select(CalendarInviteRequest).where(
            col(CalendarInviteRequest.organisation_id) == self.organisation_id,
            col(CalendarInviteRequest.requested_by_user_id) == self.requested_by_user_id,
            col(CalendarInviteRequest.idempotency_key) == idempotency_key,
        )
        result = await self.session.execute(stmt)
        return cast(CalendarInviteRequest | None, result.scalars().first())

    async def _ensure_deal_access(self, deal_id: UUID | None) -> None:
        if deal_id is None:
            return
        # Deals are soft-deleted with is_deleted; blocked deleted deals must not
        # receive outbound calendar scheduling to avoid stale business links.
        stmt = select(CRMDeal).where(
            col(CRMDeal.organisation_id) == self.organisation_id,
            col(CRMDeal.deal_id) == deal_id,
            col(CRMDeal.is_deleted) == False,  # noqa: E712
        )
        result = await self.session.execute(stmt)
        if result.scalar_one_or_none() is None:
            raise CalendarInviteError(
                "deal_not_found",
                "The requested deal does not exist in this organisation.",
                status_code=404,
            )

    async def _mark_failed(
        self,
        audit_row: CalendarInviteRequest,
        exc: CalendarInviteError,
    ) -> None:
        # Failure path is idempotent, durable, and best-effort.
        # Persisting status+error lets users and jobs know why retries stopped.
        audit_row.status = CalendarInviteRequestStatus.FAILED
        audit_row.error_code = exc.code
        audit_row.error_message = exc.message
        audit_row.updated_at = datetime.now(UTC)
        try:
            # Best-effort failure persistence: if committing a FAILED row fails,
            # rollback leaves the caller path unchanged while preserving original
            # error context in logs.
            await self.session.commit()
        except Exception:
            logger.exception("Failed to persist calendar invite failure state")
            await self.session.rollback()
