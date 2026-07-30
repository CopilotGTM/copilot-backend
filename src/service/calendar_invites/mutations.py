"""Organizer-owned calendar meeting mutation orchestration.

General Agent tools call this service after preview confirmation. The service
resolves the mailbox-specific provider event ID through current-user calendar
state, rejects recurring or attendee-only events, writes the provider first,
then mirrors the accepted result through the existing calendar writer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from sqlmodel import col
from src.domain.models.organisation_tables.sync import (
    IntegrationActivityRequest,
    IntegrationActivityRequestStatus,
    IntegrationType,
    KnowledgeIntegration,
)
from src.domain.models.organisation_tables.sync.calendar import (
    CalendarEvent,
    CalendarEventUserState,
)
from src.infra.utils.logger import get_logger
from src.service.calendar_invites.google_calendar_client import GoogleCalendarInviteClient
from src.service.calendar_invites.microsoft_graph_client import MicrosoftCalendarInviteClient
from src.service.calendar_invites.models import (
    CalendarInviteError,
    CalendarMeetingMutationResult,
    CalendarMeetingUpdateRequest,
)
from src.service.calendar_invites.resolver import CalendarIntegrationResolver
from src.service.integrations.calendar.normalizer import CalendarEventNormalizer
from src.service.integrations.calendar.types import NormalizedCalendarEvent
from src.service.integrations.calendar.writer import CalendarSyncWriter

CALENDAR_UPDATE_ACTIVITY_TYPE = "update_calendar_meeting"
CALENDAR_CANCEL_ACTIVITY_TYPE = "cancel_calendar_meeting"
CALENDAR_PARENT_TYPES = (IntegrationType.GOOGLE, IntegrationType.MICROSOFT)
logger = get_logger(__name__)


@dataclass(frozen=True)
class CalendarMutationTarget:
    """Current-user calendar row plus the provider identity required to mutate it."""

    event: CalendarEvent
    state: CalendarEventUserState
    integration: KnowledgeIntegration
    provider: str


@dataclass(frozen=True)
class CalendarMutationSnapshot:
    """Organizer provider copy used to preview and version one mutation."""

    target: CalendarMutationTarget
    provider_payload: dict[str, Any]
    normalized: NormalizedCalendarEvent
    provider_etag: str


class CalendarMeetingMutationService:
    """Update or cancel single meetings owned by the authenticated organizer."""

    def __init__(
        self,
        session,
        *,
        organisation_id: UUID,
        requested_by_user_id: UUID,
        integration_resolver: CalendarIntegrationResolver | None = None,
        google_client: GoogleCalendarInviteClient | None = None,
        microsoft_client: MicrosoftCalendarInviteClient | None = None,
        normalizer: CalendarEventNormalizer | None = None,
        writer_factory: Any = CalendarSyncWriter,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.requested_by_user_id = requested_by_user_id
        self.integration_resolver = integration_resolver or CalendarIntegrationResolver()
        self.google_client = google_client or GoogleCalendarInviteClient()
        self.microsoft_client = microsoft_client or MicrosoftCalendarInviteClient()
        self.normalizer = normalizer or CalendarEventNormalizer()
        self.writer_factory = writer_factory

    async def resolve_target(
        self,
        event_id: UUID,
        *,
        sender_email: str | None = None,
    ) -> CalendarMutationTarget:
        """Resolve one event through the current user's active calendar integration."""

        # The org/user/integration predicates are the mutation trust boundary;
        # an internal event UUID alone does not prove that this user can edit it.
        stmt = (
            select(CalendarEvent, CalendarEventUserState, KnowledgeIntegration)
            .options(selectinload(cast(Any, CalendarEvent).attendees))
            .join(
                CalendarEventUserState,
                col(CalendarEventUserState.event_id) == col(CalendarEvent.event_id),
            )
            .join(
                KnowledgeIntegration,
                col(KnowledgeIntegration.integration_id)
                == col(CalendarEventUserState.integration_id),
            )
            .where(
                col(CalendarEvent.organisation_id) == self.organisation_id,
                col(CalendarEvent.event_id) == event_id,
                col(CalendarEventUserState.is_deleted).is_(False),
                col(KnowledgeIntegration.organisation_id) == self.organisation_id,
                col(KnowledgeIntegration.created_by_user_id) == self.requested_by_user_id,
                col(KnowledgeIntegration.is_active).is_(True),
                col(KnowledgeIntegration.integration_type).in_(CALENDAR_PARENT_TYPES),
            )
        )
        if sender_email:
            stmt = stmt.where(
                func.lower(col(KnowledgeIntegration.integration_email))
                == sender_email.strip().lower()
            )
        result = await self.session.execute(stmt)
        rows = result.all()
        if not rows:
            raise CalendarInviteError(
                "calendar_meeting_not_found",
                "I could not find this meeting in your connected calendar.",
                status_code=404,
            )
        if len(rows) > 1 and not sender_email:
            raise CalendarInviteError(
                "calendar_sender_selection_required",
                "Choose which connected calendar should update this meeting.",
                status_code=409,
            )
        event, state, integration = rows[0]
        provider_payload = event.data_payload if isinstance(event.data_payload, dict) else {}
        is_series_master = (
            bool(provider_payload.get("recurrence"))
            or provider_payload.get("type") == "seriesMaster"
        )
        if event.occurrence_anchor or state.series_external_id or is_series_master:
            raise CalendarInviteError(
                "calendar_recurring_update_unsupported",
                "Recurring meeting changes are not supported yet.",
                status_code=409,
            )
        if event.is_all_day:
            raise CalendarInviteError(
                "calendar_all_day_update_unsupported",
                "All-day calendar event changes are not supported yet.",
                status_code=409,
            )
        organizer = (event.organiser_email or "").strip().lower()
        mailbox = (integration.integration_email or "").strip().lower()
        if not organizer or organizer != mailbox:
            raise CalendarInviteError(
                "calendar_organizer_required",
                "You can only change meetings organized by your connected calendar.",
                status_code=403,
            )
        provider = (
            "google" if integration.integration_type == IntegrationType.GOOGLE else "microsoft"
        )
        return CalendarMutationTarget(event, state, integration, provider)

    async def resolve_snapshot(
        self,
        event_id: UUID,
        *,
        sender_email: str | None = None,
    ) -> CalendarMutationSnapshot:
        """Fetch the organizer copy that must supply both preview data and ETag."""

        target = await self.resolve_target(event_id, sender_email=sender_email)
        resolved = await self.integration_resolver.resolve(
            self.session,
            self.organisation_id,
            self.requested_by_user_id,
            sender_email=target.integration.integration_email,
        )
        # CalendarEvent is shared across mailbox copies, so its raw payload cannot
        # safely version a write to this organizer-specific provider event ID.
        if target.provider == "google":
            payload = await self.google_client.get_event(
                resolved.token_info,
                target.state.external_id,
            )
        else:
            payload = await self.microsoft_client.get_event(
                resolved.token_info,
                target.state.external_id,
            )
        etag = payload.get("etag") or payload.get("@odata.etag")
        if not isinstance(etag, str) or not etag:
            raise CalendarInviteError(
                "calendar_meeting_version_unavailable",
                "I could not verify the latest version of this meeting. Please try again.",
                status_code=502,
            )
        return CalendarMutationSnapshot(
            target=target,
            provider_payload=payload,
            normalized=self.normalizer.normalize(payload, target.provider),
            provider_etag=etag,
        )

    async def update_meeting(
        self,
        request: CalendarMeetingUpdateRequest,
        *,
        sender_email: str | None = None,
    ) -> CalendarMeetingMutationResult:
        """Apply one confirmed complete-state update and mirror its provider response."""

        self._validate_boundary(request)
        key = self._idempotency_key(
            CALENDAR_UPDATE_ACTIVITY_TYPE,
            f"{sender_email or ''}|{request.model_dump_json()}",
        )
        existing = await self._find_operation(CALENDAR_UPDATE_ACTIVITY_TYPE, key)
        if existing:
            return self._replay_result(existing, request.event_id, "updated")
        target = await self.resolve_target(request.event_id, sender_email=sender_email)
        if not request.provider_etag:
            raise CalendarInviteError(
                "calendar_meeting_confirmation_refresh_required",
                "Please review the latest meeting details before confirming this update.",
                status_code=409,
            )
        resolved = await self.integration_resolver.resolve(
            self.session,
            self.organisation_id,
            self.requested_by_user_id,
            sender_email=target.integration.integration_email,
        )
        audit = self._audit_row(
            target,
            activity_type=CALENDAR_UPDATE_ACTIVITY_TYPE,
            idempotency_key=key,
            title=request.title,
            request_payload=request.model_dump(mode="json"),
        )
        self.session.add(audit)
        await self._commit_sending(audit)
        try:
            if target.provider == "google":
                provider_response = await self.google_client.update_event(
                    resolved.token_info, target.state.external_id, request
                )
            else:
                provider_response = await self.microsoft_client.update_event(
                    resolved.token_info, target.state.external_id, request
                )
            normalized = self.normalizer.normalize(provider_response, target.provider)
            writer = self.writer_factory(self.session, self.organisation_id)
            event = await writer.upsert_event(target.integration, normalized)
            if normalized.user_external_id:
                await writer.upsert_user_state(
                    event,
                    target.integration,
                    normalized.user_external_id,
                    normalized.series_external_id,
                )
            await writer.sync_attendees(event, normalized)
            await self._commit_success(audit, provider_response)
            return CalendarMeetingMutationResult(
                request_id=audit.request_id,
                event_id=event.event_id,
                provider_event_id=target.state.external_id,
                status="updated",
                provider_response=provider_response,
            )
        except CalendarInviteError as exc:
            await self._commit_failure(audit, exc)
            raise
        except Exception as exc:
            logger.exception("Calendar meeting update failed")
            wrapped = CalendarInviteError(
                "calendar_meeting_update_failed",
                "Could not update the meeting.",
                status_code=502,
            )
            await self._commit_failure(audit, wrapped)
            raise wrapped from exc

    async def cancel_meeting(
        self,
        event_id: UUID,
        *,
        sender_email: str | None = None,
        cancellation_message: str | None = None,
        expected_version: str | None = None,
    ) -> CalendarMeetingMutationResult:
        """Cancel one confirmed organizer meeting and reconcile its local user state."""

        payload = {
            "event_id": str(event_id),
            "sender_email": sender_email,
            "cancellation_message": cancellation_message,
            "expected_version": expected_version,
        }
        key = self._idempotency_key(
            CALENDAR_CANCEL_ACTIVITY_TYPE,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
        )
        existing = await self._find_operation(CALENDAR_CANCEL_ACTIVITY_TYPE, key)
        if existing:
            return self._replay_result(existing, event_id, "cancelled")
        target = await self.resolve_target(event_id, sender_email=sender_email)
        if not expected_version:
            raise CalendarInviteError(
                "calendar_meeting_confirmation_refresh_required",
                "Please review the latest meeting details before confirming cancellation.",
                status_code=409,
            )
        resolved = await self.integration_resolver.resolve(
            self.session,
            self.organisation_id,
            self.requested_by_user_id,
            sender_email=target.integration.integration_email,
        )
        audit = self._audit_row(
            target,
            activity_type=CALENDAR_CANCEL_ACTIVITY_TYPE,
            idempotency_key=key,
            title=target.event.title or "Meeting",
            request_payload=payload,
        )
        self.session.add(audit)
        await self._commit_sending(audit)
        try:
            if target.provider == "google":
                await self.google_client.cancel_event(
                    resolved.token_info,
                    target.state.external_id,
                    expected_version,
                )
            else:
                await self.microsoft_client.cancel_event(
                    resolved.token_info,
                    target.state.external_id,
                    cancellation_message,
                    expected_version,
                )
            writer = self.writer_factory(self.session, self.organisation_id)
            await writer.soft_delete_user_state(
                target.event,
                integration_id=target.integration.integration_id,
                external_id=target.state.external_id,
            )
            # Organizer cancellation is global provider state, unlike one
            # attendee copy disappearing during sync. Reflect it immediately.
            target.event.status = "cancelled"
            target.event.synced_at = datetime.now(UTC)
            self.session.add(target.event)
            await self._commit_success(audit, None)
            return CalendarMeetingMutationResult(
                request_id=audit.request_id,
                event_id=target.event.event_id,
                provider_event_id=target.state.external_id,
                status="cancelled",
            )
        except CalendarInviteError as exc:
            await self._commit_failure(audit, exc)
            raise
        except Exception as exc:
            logger.exception("Calendar meeting cancellation failed")
            wrapped = CalendarInviteError(
                "calendar_meeting_cancel_failed",
                "Could not cancel the meeting.",
                status_code=502,
            )
            await self._commit_failure(audit, wrapped)
            raise wrapped from exc

    def _validate_boundary(self, request: CalendarMeetingUpdateRequest) -> None:
        if (
            request.organisation_id != self.organisation_id
            or request.requested_by_user_id != self.requested_by_user_id
        ):
            raise CalendarInviteError(
                "calendar_boundary_mismatch",
                "Meeting update does not match the current user and organisation.",
                status_code=403,
            )

    def _audit_row(
        self,
        target: CalendarMutationTarget,
        *,
        activity_type: str,
        idempotency_key: str,
        title: str,
        request_payload: dict[str, Any],
    ) -> IntegrationActivityRequest:
        return IntegrationActivityRequest(
            organisation_id=self.organisation_id,
            requested_by_user_id=self.requested_by_user_id,
            integration_id=target.integration.integration_id,
            activity_type=activity_type,
            idempotency_key=idempotency_key,
            title=title,
            provider_resource_id=target.state.external_id,
            linked_record_type="calendar_event",
            linked_record_id=target.event.event_id,
            request_payload=request_payload,
            status=IntegrationActivityRequestStatus.PENDING,
            history=[{"status": "PENDING", "at": datetime.now(UTC).isoformat()}],
        )

    @staticmethod
    def _idempotency_key(activity_type: str, payload: str) -> str:
        return hashlib.sha256(f"{activity_type}|{payload}".encode()).hexdigest()[:64]

    async def _find_operation(
        self,
        activity_type: str,
        idempotency_key: str,
    ) -> IntegrationActivityRequest | None:
        stmt = select(IntegrationActivityRequest).where(
            col(IntegrationActivityRequest.organisation_id) == self.organisation_id,
            col(IntegrationActivityRequest.requested_by_user_id) == self.requested_by_user_id,
            col(IntegrationActivityRequest.activity_type) == activity_type,
            col(IntegrationActivityRequest.idempotency_key) == idempotency_key,
        )
        result = await self.session.execute(stmt)
        return cast(IntegrationActivityRequest | None, result.scalars().first())

    @staticmethod
    def _replay_result(
        existing: IntegrationActivityRequest,
        event_id: UUID,
        completed_status: str,
    ) -> CalendarMeetingMutationResult:
        status = (
            existing.status.value
            if isinstance(existing.status, IntegrationActivityRequestStatus)
            else str(existing.status)
        )
        if status == IntegrationActivityRequestStatus.FAILED.value:
            # A previous provider rejection is known, not ambiguous. Returning its
            # concrete reason lets the agent refresh the provider copy and continue.
            raise CalendarInviteError(
                existing.error_code or "calendar_meeting_mutation_failed",
                existing.error_message or "The previous meeting change was not applied.",
                status_code=409,
            )
        if status != IntegrationActivityRequestStatus.SENT.value:
            raise CalendarInviteError(
                "calendar_meeting_mutation_status_unknown",
                "This meeting change is still in progress. Please refresh the meeting.",
                status_code=409,
            )
        return CalendarMeetingMutationResult(
            request_id=existing.request_id,
            event_id=event_id,
            provider_event_id=existing.provider_resource_id or "",
            status=completed_status,
            provider_response=existing.provider_response,
        )

    async def _commit_sending(self, audit: IntegrationActivityRequest) -> None:
        audit.status = IntegrationActivityRequestStatus.SENDING
        audit.history = [*audit.history, {"status": "SENDING", "at": datetime.now(UTC).isoformat()}]
        await self.session.commit()

    async def _commit_success(
        self,
        audit: IntegrationActivityRequest,
        provider_response: dict[str, Any] | None,
    ) -> None:
        audit.status = IntegrationActivityRequestStatus.SENT
        audit.provider_response = provider_response
        audit.history = [*audit.history, {"status": "SENT", "at": datetime.now(UTC).isoformat()}]
        audit.updated_at = datetime.now(UTC)
        await self.session.commit()

    async def _commit_failure(
        self,
        audit: IntegrationActivityRequest,
        exc: CalendarInviteError,
    ) -> None:
        audit.status = IntegrationActivityRequestStatus.FAILED
        audit.error_code = exc.code
        audit.error_message = exc.message
        audit.history = [
            *audit.history,
            {"status": "FAILED", "at": datetime.now(UTC).isoformat(), "error_code": exc.code},
        ]
        audit.updated_at = datetime.now(UTC)
        await self.session.commit()
