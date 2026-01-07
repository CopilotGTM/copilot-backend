"""
Calendar sync service for Google Calendar, Outlook Calendar, etc.

This service ONLY writes to:
1. calendar_events - Canonical event records (one per unique event per org)
2. calendar_event_user_states - Tracks which integrations synced each event
3. calendar_attendees - Attendee records
4. integration_sync_history - Sync tracking

IMPORTANT: This service does NOT create OrganisationSource or OrganisationDocument records.
Calendar data is stored in dedicated calendar tables, NOT in the knowledge base/embeddings system.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.calendar import (
    CalendarAttendee,
    CalendarEvent,
    CalendarEventUserState,
)
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationSyncHistory,
    SyncStatus,
)
from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.utils.sync_metadata import (
    SyncAction,
    SyncActionCounts,
    extract_sync_action,
)

logger = get_logger(__name__)


ISO_UTC_SUFFIX = "+00:00"
DEFAULT_EVENT_TITLE = "(No title)"


def datetime_now_naive() -> datetime:
    """Return current UTC timestamp without timezone info for naive columns."""
    return datetime.now(UTC).replace(tzinfo=None)


class CalendarSyncService:
    """
    Service to sync calendar events from external providers into calendar_events table.

    Scope: This service ONLY modifies calendar-specific tables.
    - calendar_events
    - calendar_attendees
    - integration_sync_history

    This service does NOT touch:
    - organisation_sources
    - organisations_source_knowledge
    - organisation_documents
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.db = session
        self.organisation_id = organisation_id

    async def sync_calendar_events(
        self,
        *,
        integration: KnowledgeIntegration,
        events: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, Any]:
        """
        Sync calendar events from external provider.

        Tables Modified:
        - calendar_events (primary)
        - calendar_attendees (via _sync_attendees)
        - integration_sync_history (tracking)
        - knowledge_integrations (last_synced_at update only)

        Tables NOT Modified:
        - organisation_sources (not used for calendar)
        - organisations_source_knowledge (not used for calendar)
        - organisation_documents (not used for calendar)

        Args:
            integration: The integration record
            events: List of event dictionaries from provider
            provider: Provider name (google, microsoft, etc.)

        Returns:
            Dictionary with sync statistics
        """
        sync_history = await self._create_sync_history(integration.integration_id)

        try:
            event_list = list(events)
            logger.info(
                "sync_calendar_events started: integration=%s provider=%s event_count=%d",
                integration.integration_id,
                provider,
                len(event_list),
            )

            records_processed = 0
            records_failed = 0
            created_count = 0
            updated_count = 0

            for idx, event_data in enumerate(event_list):
                try:
                    external_id = self._extract_external_id(event_data, provider)
                    if not external_id:
                        logger.debug("Event %d: skipping (no external_id)", idx)
                        records_failed += 1
                        continue

                    # Upsert event to handle race conditions
                    event = await self._upsert_event(integration, event_data, provider, external_id)

                    if event.created_at == event.updated_at:
                        created_count += 1
                        logger.debug(
                            "Created event: external_id=%s title=%s",
                            external_id,
                            event.title,
                        )
                    else:
                        updated_count += 1
                        logger.debug(
                            "Updated event: external_id=%s title=%s",
                            external_id,
                            event.title,
                        )

                    records_processed += 1

                    # Commit in batches of 100 to reduce transaction overhead
                    if records_processed % 100 == 0:
                        await self.db.commit()
                        logger.debug("Batch commit: %d events processed", records_processed)

                except Exception as e:
                    logger.error(
                        "Failed to sync event %d (external_id=%s): %s",
                        idx,
                        event_data.get("id"),
                        str(e),
                        exc_info=True,
                    )
                    records_failed += 1

            # Final commit
            await self.db.commit()

            # Update sync history
            await self._complete_sync_history(
                sync_history,
                status=SyncStatus.SUCCESS,
                records_processed=records_processed,
                records_failed=records_failed,
                metadata={
                    "created": created_count,
                    "updated": updated_count,
                    "provider": provider,
                },
            )

            # Update integration last_synced_at
            integration.last_synced_at = datetime_now_naive()
            integration.last_sync_status = SyncStatus.SUCCESS.value
            self.db.add(integration)
            await self.db.commit()

            logger.info(
                "Calendar sync completed: integration=%s created=%d updated=%d failed=%d",
                integration.integration_id,
                created_count,
                updated_count,
                records_failed,
            )

            return {
                "status": "success",
                "records_processed": records_processed,
                "records_failed": records_failed,
                "created": created_count,
                "updated": updated_count,
            }

        except Exception as e:
            logger.error(
                "Calendar sync failed: integration=%s error=%s",
                integration.integration_id,
                str(e),
                exc_info=True,
            )

            await self._complete_sync_history(
                sync_history,
                status=SyncStatus.FAILED,
                records_processed=0,
                records_failed=len(event_list),
                error_details={"error": str(e), "error_type": type(e).__name__},
            )

            # Update integration sync status
            integration.last_sync_status = SyncStatus.FAILED.value
            integration.last_sync_error = str(e)[:500]
            self.db.add(integration)
            await self.db.commit()

            raise

    async def _create_sync_history(self, integration_id: UUID) -> IntegrationSyncHistory:
        """Create a new sync history record."""
        sync_history = IntegrationSyncHistory(
            integration_id=integration_id,
            started_at=datetime_now_naive(),
            status=SyncStatus.IN_PROGRESS,
        )
        self.db.add(sync_history)
        await self.db.commit()
        await self.db.refresh(sync_history)

        logger.debug("Created sync history: sync_id=%s", sync_history.sync_id)
        return sync_history

    async def _complete_sync_history(
        self,
        sync_history: IntegrationSyncHistory,
        status: SyncStatus,
        records_processed: int,
        records_failed: int,
        metadata: dict[str, Any] | None = None,
        error_details: dict[str, Any] | None = None,
    ) -> None:
        """Update sync history with completion details."""
        sync_history.completed_at = datetime_now_naive()
        sync_history.status = status
        sync_history.records_processed = records_processed
        sync_history.records_failed = records_failed

        if metadata:
            sync_history.sync_metadata = metadata

        if error_details:
            sync_history.error_details = error_details

        self.db.add(sync_history)
        await self.db.commit()

        logger.debug(
            "Updated sync history: sync_id=%s status=%s processed=%d failed=%d",
            sync_history.sync_id,
            status.value,
            records_processed,
            records_failed,
        )

    async def _upsert_event(
        self,
        integration: KnowledgeIntegration,
        event_data: dict[str, Any],
        provider: str,
        external_id: str,
    ) -> CalendarEvent:
        """
        Upsert calendar event to handle race conditions.

        Uses PostgreSQL ON CONFLICT DO UPDATE to atomically insert or update the event.
        This prevents race conditions when multiple users sync the same event simultaneously.
        """
        # Prepare event fields
        fields = self._extract_event_fields(event_data, provider)

        # Determine if current integration user is the organizer
        organiser_email = fields.get("organiser_email")
        is_organizer = (
            organiser_email
            and integration.integration_email
            and organiser_email.lower() == integration.integration_email.lower()
        )

        # Base values for insert
        values = {
            "organisation_id": self.organisation_id,
            "integration_id": integration.integration_id,  # Default to current integration
            "external_id": external_id,
            "data_payload": event_data,
            "synced_at": datetime.now(UTC),
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
            **fields,
        }

        # Construct upsert statement
        stmt = insert(CalendarEvent).values(**values)

        # Prepare update values (exclude primary key and immutable fields)
        update_dict = {
            k: v
            for k, v in values.items()
            if k not in ("event_id", "organisation_id", "external_id", "created_at")
        }

        # Special handling for integration_id on update:
        # Only update integration_id if the current user is the organizer.
        # Otherwise, keep the existing integration_id.
        if not is_organizer:
            update_dict.pop("integration_id")

        # Add ON CONFLICT clause
        stmt = stmt.on_conflict_do_update(
            index_elements=["organisation_id", "external_id"],
            set_=update_dict,
        ).returning(CalendarEvent)

        # Execute upsert
        result = await self.db.execute(stmt)
        event = result.scalars().one()

        # Sync attendees (always needed)
        await self._sync_attendees(event, event_data)

        # Upsert user state to track which integrations have this event
        await self._upsert_user_state(event, integration, external_id)

        return event

    async def _upsert_user_state(
        self,
        event: CalendarEvent,
        integration: KnowledgeIntegration,
        external_id: str,
    ) -> CalendarEventUserState:
        """
        Upsert calendar event user state to track which integrations synced this event.
        Used for cleanup when a user disconnects their integration.
        """
        values = {
            "event_id": event.event_id,
            "integration_id": integration.integration_id,
            "external_id": external_id,
            "is_deleted": False,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }

        stmt = insert(CalendarEventUserState).values(**values)

        # Prepare update values (exclude primary key and immutable fields)
        update_dict = {
            "external_id": external_id,
            "is_deleted": False,
            "updated_at": datetime.now(UTC),
        }

        stmt = stmt.on_conflict_do_update(
            index_elements=["event_id", "integration_id"],
            set_=update_dict,
        ).returning(CalendarEventUserState)

        result = await self.db.execute(stmt)
        return result.scalars().one()

    async def _sync_attendees(self, event: CalendarEvent, event_data: dict[str, Any]) -> None:
        """Sync attendees for an event."""
        attendees_data = event_data.get("attendees", [])
        if not attendees_data:
            return

        # Remove existing attendees so we can insert the fresh snapshot without violating the
        # unique (event_id, email) constraint.
        from sqlalchemy import delete

        await self.db.execute(
            delete(CalendarAttendee).where(CalendarAttendee.event_id == event.event_id)  # type: ignore[arg-type]
        )

        # Get organizer email to identify who is the organizer in attendees
        organizer_email = self._extract_organizer_email(event_data)

        # Create new attendees
        for attendee_data in attendees_data:
            email = self._extract_attendee_email(attendee_data)
            if not email:
                continue

            display_name = self._extract_attendee_name(attendee_data)
            response_status = self._extract_attendee_response(attendee_data)

            # Check if this attendee is the organizer
            # Google: has "organizer": true in attendee data
            # Microsoft: match email with top-level organizer.emailAddress.address
            is_organiser = attendee_data.get("organizer", False) or (
                organizer_email and email.lower() == organizer_email.lower()
            )

            attendee = CalendarAttendee(
                event_id=event.event_id,
                email=email,
                display_name=display_name,
                response_status=self._normalize_response_status(response_status),
                is_organiser=is_organiser,
                data_payload=attendee_data,
            )
            self.db.add(attendee)
        await self.db.flush()

    @staticmethod
    def _extract_attendee_email(attendee_data: dict[str, Any]) -> str | None:
        """Extract email from attendee data (handles Google and Microsoft formats)."""
        # Google format: email at top level
        email = attendee_data.get("email")
        if email:
            return email

        # Microsoft/Outlook format: nested in emailAddress
        email_obj = attendee_data.get("emailAddress")
        if isinstance(email_obj, dict):
            return email_obj.get("address")

        return None

    @staticmethod
    def _extract_attendee_name(attendee_data: dict[str, Any]) -> str | None:
        """Extract display name from attendee data (handles Google and Microsoft formats)."""
        # Google format: displayName at top level
        display_name = attendee_data.get("displayName")
        if display_name:
            return display_name

        # Microsoft/Outlook format: nested in emailAddress
        email_obj = attendee_data.get("emailAddress")
        if isinstance(email_obj, dict):
            return email_obj.get("name")

        return None

    @staticmethod
    def _extract_attendee_response(attendee_data: dict[str, Any]) -> str:
        """Extract response status from attendee data (handles Google and Microsoft formats)."""
        # Google format: responseStatus as string at top level
        response_status = attendee_data.get("responseStatus")
        if isinstance(response_status, str):
            return response_status

        # Microsoft format: status object with response field
        status_obj = attendee_data.get("status")
        if isinstance(status_obj, dict):
            return status_obj.get("response", "none")

        return "none"

    @staticmethod
    def _extract_organizer_email(event_data: dict[str, Any]) -> str | None:
        """Extract organizer email from event data (handles Google and Microsoft formats)."""
        organizer = event_data.get("organizer")
        if not organizer:
            return None

        # Google format: organizer.email
        email = organizer.get("email")
        if email:
            return email

        # Microsoft/Outlook format: organizer.emailAddress.address
        email_obj = organizer.get("emailAddress")
        if isinstance(email_obj, dict):
            return email_obj.get("address")

        return None

    @staticmethod
    def _extract_external_id(event_data: dict[str, Any], provider: str) -> str | None:
        """Extract external ID from event data."""
        if provider == "google":
            return event_data.get("id") or event_data.get("eventId")
        elif provider in ("microsoft", "outlook"):
            return event_data.get("id") or event_data.get("iCalUId")
        return event_data.get("id")

    @classmethod
    def _extract_event_fields(cls, event_data: dict[str, Any], provider: str) -> dict[str, Any]:
        """Extract event fields from provider data."""
        logger.debug("Extracting event fields for provider=%s", provider)
        if provider == "google":
            return cls._extract_google_event_fields(event_data)
        if provider in ("microsoft", "outlook"):
            return cls._extract_microsoft_event_fields(event_data)
        logger.warning("Unknown provider %s, using generic extraction", provider)
        return cls._extract_generic_event_fields(event_data)

    @classmethod
    def _extract_google_event_fields(cls, event_data: dict[str, Any]) -> dict[str, Any]:
        fields = {
            "title": event_data.get("summary") or DEFAULT_EVENT_TITLE,
            "description": event_data.get("description"),
            "location": event_data.get("location"),
            "status": event_data.get("status"),
            "calendar_external_id": event_data.get("calendarId"),
        }
        cls._apply_google_time_fields(fields, event_data)
        cls._apply_google_conference(fields, event_data.get("conferenceData"))
        cls._apply_google_organizer(fields, event_data.get("organizer"))
        cls._apply_attendee_count(fields, event_data.get("attendees"))
        cls._apply_google_source_modified(fields, event_data)
        return fields

    @classmethod
    def _apply_google_time_fields(cls, fields: dict[str, Any], event_data: dict[str, Any]) -> None:
        start = event_data.get("start")
        if isinstance(start, dict):
            cls._set_google_start(fields, start)
            fields["timezone"] = start.get("timeZone")

        end = event_data.get("end")
        if isinstance(end, dict):
            cls._set_google_end(fields, end)

    @classmethod
    def _set_google_start(cls, fields: dict[str, Any], start: dict[str, Any]) -> None:
        if "dateTime" in start:
            fields["start_at"] = cls._parse_iso_datetime(start["dateTime"])
            fields["is_all_day"] = False
        elif "date" in start:
            fields["start_at"] = cls._parse_iso_date(start["date"])
            fields["is_all_day"] = True

    @classmethod
    def _set_google_end(cls, fields: dict[str, Any], end: dict[str, Any]) -> None:
        if "dateTime" in end:
            fields["end_at"] = cls._parse_iso_datetime(end["dateTime"])
        elif "date" in end:
            fields["end_at"] = cls._parse_iso_date(end["date"])

    @staticmethod
    def _apply_google_conference(fields: dict[str, Any], conference_data: Any) -> None:
        if not isinstance(conference_data, dict):
            return

        # Extract video conference link
        for entry in conference_data.get("entryPoints", []):
            if entry.get("entryPointType") == "video":
                fields["conference_link"] = entry.get("uri")
                break

        # Extract online meeting provider and ID
        solution = conference_data.get("conferenceSolution", {})
        solution_type = solution.get("key", {}).get("type", "")

        if solution_type == "hangoutsMeet":
            fields["online_meeting_provider"] = "googleMeet"
            # Google Meet conferenceId is the meeting code (e.g., "abc-defg-hij")
            fields["online_meeting_id"] = conference_data.get("conferenceId")
        elif solution_type == "addOn":
            # Third-party provider integrated via Google Calendar (e.g., Zoom)
            provider_name = solution.get("name", "").lower().replace(" ", "")
            fields["online_meeting_provider"] = provider_name or "thirdParty"
            fields["online_meeting_id"] = conference_data.get("conferenceId")

    @staticmethod
    def _apply_google_organizer(fields: dict[str, Any], organizer: Any) -> None:
        if isinstance(organizer, dict):
            fields["organiser_email"] = organizer.get("email")
            fields["organiser_name"] = organizer.get("displayName")

    @classmethod
    def _apply_google_source_modified(
        cls, fields: dict[str, Any], event_data: dict[str, Any]
    ) -> None:
        updated = event_data.get("updated")
        if updated:
            fields["source_modified_at"] = cls._parse_iso_datetime(updated)

    @classmethod
    def _extract_microsoft_event_fields(cls, event_data: dict[str, Any]) -> dict[str, Any]:
        # Microsoft uses isCancelled boolean; translate to status string for consistency with Google
        is_cancelled = event_data.get("isCancelled", False)
        status = "cancelled" if is_cancelled else "confirmed"
        logger.debug(
            "Microsoft event extraction: isCancelled=%s -> status=%s, subject=%s",
            is_cancelled,
            status,
            event_data.get("subject"),
        )

        fields = {
            "title": event_data.get("subject") or DEFAULT_EVENT_TITLE,
            "description": event_data.get("bodyPreview")
            or event_data.get("body", {}).get("content"),
            "location": event_data.get("location", {}).get("displayName"),
            "status": status,
        }
        cls._apply_microsoft_times(fields, event_data)
        cls._apply_microsoft_organizer(fields, event_data)
        cls._apply_microsoft_online_meeting(fields, event_data)
        cls._apply_attendee_count(fields, event_data.get("attendees"))
        cls._apply_microsoft_source_modified(fields, event_data)
        return fields

    @classmethod
    def _apply_microsoft_times(cls, fields: dict[str, Any], event_data: dict[str, Any]) -> None:
        start = event_data.get("start")
        if isinstance(start, dict):
            date_time = start.get("dateTime")
            if date_time:
                fields["start_at"] = cls._parse_iso_datetime(date_time)
            fields["timezone"] = start.get("timeZone")
            fields["is_all_day"] = event_data.get("isAllDay", False)

        end = event_data.get("end")
        if isinstance(end, dict):
            date_time = end.get("dateTime")
            if date_time:
                fields["end_at"] = cls._parse_iso_datetime(date_time)

    @staticmethod
    def _apply_microsoft_organizer(fields: dict[str, Any], event_data: dict[str, Any]) -> None:
        organizer = event_data.get("organizer", {}).get("emailAddress", {})
        if organizer:
            fields["organiser_email"] = organizer.get("address")
            fields["organiser_name"] = organizer.get("name")

    @staticmethod
    def _apply_microsoft_online_meeting(fields: dict[str, Any], event_data: dict[str, Any]) -> None:
        online_meeting = event_data.get("onlineMeeting")
        if not isinstance(online_meeting, dict):
            return

        join_url = online_meeting.get("joinUrl")
        fields["conference_link"] = join_url

        # Extract Teams meeting ID from join URL for transcript access
        if join_url and "teams.microsoft.com" in join_url:
            fields["online_meeting_provider"] = "teamsForBusiness"
            meeting_id = CalendarSyncService._extract_teams_meeting_id(join_url)
            if meeting_id:
                fields["online_meeting_id"] = meeting_id

    @staticmethod
    def _extract_teams_meeting_id(join_url: str) -> str | None:
        """
        Extract Teams online meeting ID from join URL.

        The join URL contains the meeting thread ID which is needed for
        accessing transcripts via MS Graph API.

        URL format:
            https://teams.microsoft.com/l/meetup-join/
            19%3ameeting_ZGQ4ZmVlYzEtY...%40thread.v2/0?context=...

        The meeting ID is: 19:meeting_ZGQ4ZmVlYzEtY...@thread.v2

        Returns:
            The decoded meeting ID or None if parsing fails.
        """
        import urllib.parse

        try:
            parsed = urllib.parse.urlparse(join_url)
            # The path looks like: /l/meetup-join/19%3ameeting_XXX%40thread.v2/0
            path_parts = parsed.path.split("/")

            for part in path_parts:
                if not part:
                    continue
                decoded = urllib.parse.unquote(part)
                # Teams meeting IDs start with "19:meeting_" and contain "@thread"
                if decoded.startswith("19:meeting_") and "@thread" in decoded:
                    return decoded

        except Exception as e:
            logger.warning("Failed to extract Teams meeting ID from URL: %s", str(e))
            # If parsing fails, return None - we'll just not have transcript access

        return None

    @classmethod
    def _apply_microsoft_source_modified(
        cls, fields: dict[str, Any], event_data: dict[str, Any]
    ) -> None:
        modified = event_data.get("lastModifiedDateTime")
        if modified:
            fields["source_modified_at"] = cls._parse_iso_datetime(modified)

    @staticmethod
    def _extract_generic_event_fields(event_data: dict[str, Any]) -> dict[str, Any]:
        location = event_data.get("location")
        # Handle location as either string or dict
        if isinstance(location, dict):
            location = location.get("displayName")

        return {
            "title": event_data.get("summary") or event_data.get("subject") or DEFAULT_EVENT_TITLE,
            "description": event_data.get("description"),
            "location": location,
            "status": event_data.get("status"),
        }

    @staticmethod
    def _apply_attendee_count(fields: dict[str, Any], attendees: Any) -> None:
        if isinstance(attendees, list):
            fields["attendee_count"] = len(attendees)
        else:
            fields["attendee_count"] = 0

    @staticmethod
    def _parse_iso_datetime(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", ISO_UTC_SUFFIX))

    @staticmethod
    def _parse_iso_date(value: str) -> datetime:
        return datetime.fromisoformat(f"{value}T00:00:00{ISO_UTC_SUFFIX}")

    @staticmethod
    def _normalize_response_status(status: str) -> str:
        """Normalize response status to match database constraint."""
        status_map = {
            "accepted": "accepted",
            "declined": "declined",
            "tentative": "tentative",
            "needsAction": "needsAction",
            "needs_action": "needsAction",
            "none": "none",
        }
        return status_map.get(status.lower(), "none")

    # -------------------------------------------------------------------------
    # Incremental Sync Methods
    # -------------------------------------------------------------------------

    async def sync_calendar_events_incremental(
        self,
        *,
        integration: KnowledgeIntegration,
        events: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, Any]:
        """
        Sync calendar events using incremental optimization.

        This method optimizes sync by:
        - Using database state to determine if records are new or existing
        - Running change detection to skip unchanged records
        - Handling cancelled events via status field

        Args:
            integration: KnowledgeIntegration instance
            events: Raw event records with optional _sync_metadata
            provider: Provider name (google, microsoft, etc.)

        Returns:
            Dictionary with sync statistics including skipped count
        """
        sync_history = await self._create_sync_history(integration.integration_id)

        try:
            event_list = list(events)
            logger.info(
                "sync_calendar_events_incremental started: integration=%s provider=%s event_count=%d",
                integration.integration_id,
                provider,
                len(event_list),
            )

            if not event_list:
                await self._complete_sync_history(
                    sync_history,
                    status=SyncStatus.SUCCESS,
                    records_processed=0,
                    records_failed=0,
                    metadata={"provider": provider, "skipped": 0},
                )
                return {
                    "status": "success",
                    "records_processed": 0,
                    "records_failed": 0,
                    "created": 0,
                    "updated": 0,
                    "skipped": 0,
                }

            # Track action counts for logging (observability only)
            action_counts = SyncActionCounts()

            # Load existing events for change detection
            event_external_ids = [
                self._extract_external_id(e, provider)
                for e in event_list
                if self._extract_external_id(e, provider)
            ]
            existing = await self._load_existing_events_by_external_ids(event_external_ids)

            records_processed = 0
            records_failed = 0
            created_count = 0
            updated_count = 0
            skipped_count = 0

            for idx, event_data in enumerate(event_list):
                try:
                    external_id = self._extract_external_id(event_data, provider)
                    if not external_id:
                        logger.debug("Event %d: skipping (no external_id)", idx)
                        records_failed += 1
                        continue

                    # Extract sync action for logging and deletion handling
                    sync_action = extract_sync_action(event_data)
                    if sync_action:
                        action_counts.increment(sync_action)

                    # Check database state - this is the source of truth
                    record = existing.get(external_id)
                    is_new = record is None

                    # Check if event is cancelled/deleted
                    # Google: status == "cancelled"
                    # Microsoft: isCancelled == true (already mapped to status: "cancelled")
                    fields = self._extract_event_fields(event_data, provider)
                    is_cancelled = fields.get("status") == "cancelled"

                    # Handle cancelled events
                    if is_cancelled or sync_action == SyncAction.DELETED:
                        if record:
                            # Update status to cancelled for existing events
                            record.status = "cancelled"
                            record.synced_at = datetime.now(UTC)
                            self.db.add(record)
                            records_processed += 1
                            logger.debug(
                                "Marked event as cancelled: external_id=%s",
                                external_id,
                            )
                        continue

                    # Handle new records
                    if is_new:
                        event = await self._upsert_event(
                            integration, event_data, provider, external_id
                        )
                        records_processed += 1
                        created_count += 1
                        logger.debug(
                            "Created event: external_id=%s title=%s",
                            external_id,
                            event.title,
                        )
                    else:
                        # Existing record - check for meaningful changes
                        if self._has_meaningful_event_changes(record, event_data, provider):
                            event = await self._upsert_event(
                                integration, event_data, provider, external_id
                            )
                            records_processed += 1
                            updated_count += 1
                            logger.debug(
                                "Updated event: external_id=%s title=%s",
                                external_id,
                                event.title,
                            )
                        else:
                            # No meaningful changes - skip database write
                            skipped_count += 1
                            action_counts.unchanged += 1
                            logger.debug(
                                "Skipping unchanged event: external_id=%s",
                                external_id,
                            )
                            continue

                    # Commit in batches of 100
                    if records_processed % 100 == 0:
                        await self.db.commit()
                        logger.debug("Batch commit: %d events processed", records_processed)

                except Exception as e:
                    logger.error(
                        "Failed to sync event %d (external_id=%s): %s",
                        idx,
                        event_data.get("id"),
                        str(e),
                        exc_info=True,
                    )
                    records_failed += 1

            # Final commit
            await self.db.commit()

            # Update sync history
            await self._complete_sync_history(
                sync_history,
                status=SyncStatus.SUCCESS,
                records_processed=records_processed,
                records_failed=records_failed,
                metadata={
                    "created": created_count,
                    "updated": updated_count,
                    "skipped": skipped_count,
                    "provider": provider,
                    "action_counts": action_counts.to_dict(),
                },
            )

            # Update integration last_synced_at
            integration.last_synced_at = datetime_now_naive()
            integration.last_sync_status = SyncStatus.SUCCESS.value
            self.db.add(integration)
            await self.db.commit()

            logger.info(
                "Calendar incremental sync completed: integration=%s "
                "created=%d updated=%d skipped=%d failed=%d "
                "(added=%d updated=%d deleted=%d)",
                integration.integration_id,
                created_count,
                updated_count,
                skipped_count,
                records_failed,
                action_counts.added,
                action_counts.updated,
                action_counts.deleted,
            )

            return {
                "status": "success",
                "records_processed": records_processed,
                "records_failed": records_failed,
                "created": created_count,
                "updated": updated_count,
                "skipped": skipped_count,
                "action_counts": action_counts.to_dict(),
            }

        except Exception as e:
            logger.error(
                "Calendar incremental sync failed: integration=%s error=%s",
                integration.integration_id,
                str(e),
                exc_info=True,
            )

            await self._complete_sync_history(
                sync_history,
                status=SyncStatus.FAILED,
                records_processed=0,
                records_failed=len(event_list),
                error_details={"error": str(e), "error_type": type(e).__name__},
            )

            # Update integration sync status
            integration.last_sync_status = SyncStatus.FAILED.value
            integration.last_sync_error = str(e)[:500]
            self.db.add(integration)
            await self.db.commit()

            raise

    async def _load_existing_events_by_external_ids(
        self,
        external_ids: list[str],
    ) -> dict[str, CalendarEvent]:
        """Load existing calendar events by external IDs for change detection.

        Args:
            external_ids: List of external IDs to look up

        Returns:
            Dictionary mapping external_id -> CalendarEvent
        """
        if not external_ids:
            return {}

        stmt = select(CalendarEvent).where(
            CalendarEvent.organisation_id == self.organisation_id,
            CalendarEvent.external_id.in_(external_ids),
        )
        result = await self.db.execute(stmt)
        events = result.scalars().all()

        return {e.external_id: e for e in events}

    def _has_meaningful_event_changes(
        self,
        existing: CalendarEvent,
        incoming: dict[str, Any],
        provider: str,
    ) -> bool:
        """Check if incoming event data has meaningful changes.

        Args:
            existing: Existing CalendarEvent record from database
            incoming: Incoming event data from provider
            provider: Provider name (google, microsoft, etc.)

        Returns:
            True if meaningful changes detected, False otherwise
        """
        fields = self._extract_event_fields(incoming, provider)

        # Check source_modified_at as primary indicator
        incoming_modified = fields.get("source_modified_at")
        if (
            incoming_modified
            and existing.source_modified_at
            and incoming_modified > existing.source_modified_at
        ):
            return True

        # Compare key fields
        checks = [
            (existing.title, fields.get("title")),
            (existing.description, fields.get("description")),
            (existing.location, fields.get("location")),
            (existing.status, fields.get("status")),
            (existing.conference_link, fields.get("conference_link")),
            (existing.organiser_email, fields.get("organiser_email")),
        ]

        for existing_val, incoming_val in checks:
            if existing_val is None and incoming_val is None:
                continue
            if existing_val != incoming_val:
                return True

        # Compare start_at and end_at (datetime comparison)
        incoming_start = fields.get("start_at")
        if (
            incoming_start
            and existing.start_at
            and incoming_start.replace(tzinfo=None) != existing.start_at.replace(tzinfo=None)
        ):
            return True

        incoming_end = fields.get("end_at")
        if (
            incoming_end
            and existing.end_at
            and incoming_end.replace(tzinfo=None) != existing.end_at.replace(tzinfo=None)
        ):
            return True

        # Compare attendee_count
        incoming_attendee_count = fields.get("attendee_count", 0)
        if existing.attendee_count != incoming_attendee_count:
            return True

        # Compare is_all_day
        incoming_is_all_day = fields.get("is_all_day", False)
        return existing.is_all_day != incoming_is_all_day
