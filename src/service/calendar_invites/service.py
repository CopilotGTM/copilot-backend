"""Production service for creating user-owned calendar invites."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import (
    IntegrationActivityRequest,
    IntegrationActivityRequestStatus,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.utils.logger import get_logger
from src.service.calendar_invites.google_calendar_client import GoogleCalendarInviteClient
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

CALENDAR_INVITE_ACTIVITY_TYPE = "send_calendar_invite"


def _activity_status_value(status: IntegrationActivityRequestStatus | str) -> str:
    return status.value if isinstance(status, IntegrationActivityRequestStatus) else str(status)


def derive_idempotency_key(
    request: CalendarInviteCreateRequest,
    *,
    integration_id: UUID | None = None,
) -> str:
    """Derive a stable Graph transactionId from the user intent."""
    # Include the resolved integration when available so sender changes do not
    # replay an invite created from a different calendar account.
    attendee_part = "|".join(
        sorted(
            f"{attendee.email},{attendee.name or ''},{attendee.optional}"
            for attendee in request.attendees
        )
    )
    sender_part = (request.sender_email or "").strip().lower()
    raw = "|".join(
        [
            str(request.organisation_id),
            str(request.requested_by_user_id),
            str(integration_id or ""),
            sender_part,
            request.title,
            request.body or "",
            request.location or "",
            request.start_at.isoformat(),
            request.end_at.isoformat(),
            request.timezone,
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
        google_client: GoogleCalendarInviteClient | None = None,
        normalizer: CalendarEventNormalizer | None = None,
        writer_factory: Any = CalendarSyncWriter,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.requested_by_user_id = requested_by_user_id
        self.integration_resolver = integration_resolver or OutlookCalendarIntegrationResolver()
        self.graph_client = graph_client or MicrosoftCalendarInviteClient()
        self.google_client = google_client or GoogleCalendarInviteClient()
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

        resolved = await self.integration_resolver.resolve(
            self.session,
            self.organisation_id,
            self.requested_by_user_id,
            sender_email=request.sender_email,
        )
        idempotency_key = request.idempotency_key or derive_idempotency_key(
            request,
            integration_id=resolved.integration.integration_id,
        )
        # Reuse prior successful attempts by sender-scoped idempotency key so
        # chat retry does not send duplicate invites in rapid retry loops.
        existing = await self._find_existing_request(idempotency_key)
        existing_status = _activity_status_value(existing.status) if existing else None
        if existing and not self._existing_matches_request(existing, request):
            raise CalendarInviteError(
                "calendar_invite_idempotency_conflict",
                "This idempotency key was already used for a different calendar invite request.",
                status_code=409,
            )
        if existing and existing_status == IntegrationActivityRequestStatus.SENT.value:
            return CalendarInviteCreateResult(
                request_id=existing.request_id,
                event_id=existing.linked_record_id,
                provider_event_id=existing.provider_resource_id or "",
                subject=existing.title,
                conference_link=existing.conference_link,
            )
        if existing and existing_status == IntegrationActivityRequestStatus.SENDING.value:
            raise CalendarInviteError(
                "calendar_invite_status_unknown",
                (
                    "This calendar invite is already in progress or its final provider status is "
                    "unknown. Confirm before attempting to resend."
                ),
                status_code=409,
            )
        if existing:
            raise CalendarInviteError(
                "calendar_invite_replay_blocked",
                (
                    "A previous attempt for this calendar invite already exists and was not "
                    "confirmed as sent. Confirm before attempting to resend."
                ),
                status_code=409,
            )

        await self._ensure_deal_access(request.deal_id)
        provider_timezone = (
            to_outlook_timezone(request.timezone)
            if resolved.provider == "microsoft"
            else request.timezone
        )
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
            audit_row.status = IntegrationActivityRequestStatus.SENDING
            audit_row.history = self._append_history(audit_row, "SENDING")
            audit_row.updated_at = datetime.now(UTC)
            await self.session.flush()
            await self.session.commit()

            provider_response = await self._create_provider_event(
                resolved.provider,
                resolved.token_info,
                request.model_copy(update={"idempotency_key": idempotency_key}),
            )
            provider_event_id = provider_response.get("id")
            if not isinstance(provider_event_id, str) or not provider_event_id:
                raise CalendarInviteError(
                    "calendar_provider_missing_event_id",
                    "The calendar provider created no usable calendar event ID.",
                    status_code=502,
                )

            normalized = self.normalizer.normalize(provider_response, resolved.provider)
            if not normalized.identity.external_id:
                raise CalendarInviteError(
                    "calendar_provider_missing_ical_uid",
                    "The calendar provider created the invite but returned no shared calendar UID.",
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

            audit_row.status = IntegrationActivityRequestStatus.SENT
            audit_row.provider_resource_id = provider_event_id
            audit_row.provider_response = provider_response
            audit_row.linked_record_type = "calendar_event"
            audit_row.linked_record_id = event.event_id
            audit_row.conference_link = self._extract_conference_link(
                provider_response,
                resolved.provider,
            )
            audit_row.history = self._append_history(audit_row, "SENT")
            audit_row.updated_at = datetime.now(UTC)
            await self.session.commit()

            return CalendarInviteCreateResult(
                request_id=audit_row.request_id,
                event_id=event.event_id,
                provider_event_id=provider_event_id,
                subject=provider_response.get("subject") or request.title,
                conference_link=audit_row.conference_link,
                web_link=provider_response.get("webLink") or provider_response.get("htmlLink"),
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
    ) -> IntegrationActivityRequest:
        # Keep an explicit provider_timezone column for parity with Graph payload
        # and to support future troubleshooting when timezone conversions drift.
        attendees = [attendee.model_dump() for attendee in request.attendees]
        return IntegrationActivityRequest(
            organisation_id=self.organisation_id,
            requested_by_user_id=self.requested_by_user_id,
            integration_id=integration_id,
            deal_id=request.deal_id,
            activity_type=CALENDAR_INVITE_ACTIVITY_TYPE,
            title=request.title,
            body=request.body,
            location=request.location,
            start_at=request.start_at,
            end_at=request.end_at,
            timezone=request.timezone,
            provider_timezone=request.timezone,
            participants=attendees,
            idempotency_key=request.idempotency_key or "",
            status=IntegrationActivityRequestStatus.PENDING,
            request_payload={
                "sender_email": request.sender_email,
                "attendees": attendees,
            },
            history=[self._history_entry("PENDING")],
        )

    def _existing_matches_request(
        self,
        existing: IntegrationActivityRequest,
        request: CalendarInviteCreateRequest,
    ) -> bool:
        attendees = [attendee.model_dump() for attendee in request.attendees]
        payload = dict(existing.request_payload or {})
        return (
            existing.title == request.title
            and existing.body == request.body
            and existing.location == request.location
            and existing.start_at == request.start_at
            and existing.end_at == request.end_at
            and existing.timezone == request.timezone
            and list(existing.participants or []) == attendees
            and payload.get("sender_email") == request.sender_email
            and payload.get("attendees") == attendees
        )

    async def _create_provider_event(
        self,
        provider: str,
        token_info: Any,
        request: CalendarInviteCreateRequest,
    ) -> dict[str, Any]:
        if provider == "google":
            return await self.google_client.create_event(token_info, request)
        return await self.graph_client.create_event(token_info, request)

    @staticmethod
    def _extract_conference_link(
        provider_response: dict[str, Any],
        provider: str,
    ) -> str | None:
        if provider == "google":
            conference_data = provider_response.get("conferenceData")
            if isinstance(conference_data, dict):
                for entry in conference_data.get("entryPoints", []):
                    if (
                        isinstance(entry, dict)
                        and entry.get("entryPointType") == "video"
                        and isinstance(entry.get("uri"), str)
                    ):
                        return str(entry["uri"])
            return None

        online_meeting = provider_response.get("onlineMeeting")
        if isinstance(online_meeting, dict):
            join_url = online_meeting.get("joinUrl")
            return join_url if isinstance(join_url, str) else None
        return None

    async def _find_existing_request(
        self, idempotency_key: str
    ) -> IntegrationActivityRequest | None:
        # Query by org/user/idempotency so replay from one user cannot observe
        # or collide with another user's previous attempt.
        stmt = select(IntegrationActivityRequest).where(
            col(IntegrationActivityRequest.organisation_id) == self.organisation_id,
            col(IntegrationActivityRequest.requested_by_user_id) == self.requested_by_user_id,
            col(IntegrationActivityRequest.activity_type) == CALENDAR_INVITE_ACTIVITY_TYPE,
            col(IntegrationActivityRequest.idempotency_key) == idempotency_key,
        )
        result = await self.session.execute(stmt)
        return cast(IntegrationActivityRequest | None, result.scalars().first())

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
        audit_row: IntegrationActivityRequest,
        exc: CalendarInviteError,
    ) -> None:
        # Failure path is idempotent, durable, and best-effort.
        # Persisting status+error lets users and jobs know why retries stopped.
        audit_row.status = IntegrationActivityRequestStatus.FAILED
        audit_row.error_code = exc.code
        audit_row.error_message = exc.message
        audit_row.history = self._append_history(audit_row, "FAILED", error_code=exc.code)
        audit_row.updated_at = datetime.now(UTC)
        try:
            # Best-effort failure persistence: if committing a FAILED row fails,
            # rollback leaves the caller path unchanged while preserving original
            # error context in logs.
            await self.session.commit()
        except Exception:
            logger.exception("Failed to persist calendar invite failure state")
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
