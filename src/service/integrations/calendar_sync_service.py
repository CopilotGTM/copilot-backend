"""
Calendar sync service for Google Calendar, Outlook Calendar, etc.

This service ONLY writes to:
1. calendar_events - Event records
2. calendar_attendees - Attendee records
3. integration_sync_history - Sync tracking

IMPORTANT: This service does NOT create OrganisationSource or OrganisationDocument records.
Calendar data is stored in dedicated calendar tables, NOT in the knowledge base/embeddings system.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.calendar import CalendarAttendee, CalendarEvent
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationSyncHistory,
    SyncStatus,
)
from src.infra.utils.logger import get_logger

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

                    # Check if event already exists
                    existing_event = await self._get_event_by_external_id(
                        integration.integration_id, external_id
                    )

                    if existing_event:
                        # Update existing event
                        await self._update_event(existing_event, event_data, provider)
                        updated_count += 1
                        logger.debug(
                            "Updated event: external_id=%s title=%s",
                            external_id,
                            event_data.get("summary"),
                        )
                    else:
                        # Create new event
                        await self._create_event(integration, event_data, provider, external_id)
                        created_count += 1
                        logger.debug(
                            "Created event: external_id=%s title=%s",
                            external_id,
                            event_data.get("summary"),
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

            logger.info(
                "Tables modified: calendar_events (events), calendar_attendees (attendees), "
                "integration_sync_history (tracking), knowledge_integrations (metadata only)"
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

        logger.info("Created sync history: sync_id=%s", sync_history.sync_id)
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

        logger.info(
            "Updated sync history: sync_id=%s status=%s processed=%d failed=%d",
            sync_history.sync_id,
            status.value,
            records_processed,
            records_failed,
        )

    async def _get_event_by_external_id(
        self, integration_id: UUID, external_id: str
    ) -> CalendarEvent | None:
        """Get existing calendar event by external_id."""
        stmt = select(CalendarEvent).where(
            CalendarEvent.integration_id == integration_id,
            CalendarEvent.external_id == external_id,
        )
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def _create_event(
        self,
        integration: KnowledgeIntegration,
        event_data: dict[str, Any],
        provider: str,
        external_id: str,
    ) -> CalendarEvent:
        """Create a new calendar event."""
        event = CalendarEvent(
            organisation_id=self.organisation_id,
            integration_id=integration.integration_id,
            external_id=external_id,
            data_payload=event_data,
            synced_at=datetime.now(UTC),
            **self._extract_event_fields(event_data, provider),
        )

        self.db.add(event)
        # No need to flush here; event creation/update already flushes attendees.

        # Add attendees if present
        await self._sync_attendees(event, event_data)

        return event

    async def _update_event(
        self, event: CalendarEvent, event_data: dict[str, Any], provider: str
    ) -> None:
        """Update existing calendar event."""
        # Update fields
        fields = self._extract_event_fields(event_data, provider)
        for key, value in fields.items():
            setattr(event, key, value)

        # Update metadata
        event.data_payload = event_data
        event.synced_at = datetime.now(UTC)

        self.db.add(event)
        await self.db.flush()

        # Update attendees
        await self._sync_attendees(event, event_data)

    async def _sync_attendees(self, event: CalendarEvent, event_data: dict[str, Any]) -> None:
        """Sync attendees for an event."""
        attendees_data = event_data.get("attendees", [])
        if not attendees_data:
            return

        # Remove existing attendees so we can insert the fresh snapshot without violating the
        # unique (event_id, email) constraint.
        from sqlalchemy import delete

        await self.db.execute(
            delete(CalendarAttendee).where(CalendarAttendee.event_id == event.event_id)
        )

        # Create new attendees
        for attendee_data in attendees_data:
            email = attendee_data.get("email")
            if not email:
                continue

            attendee = CalendarAttendee(
                event_id=event.event_id,
                email=email,
                display_name=attendee_data.get("displayName"),
                response_status=self._normalize_response_status(
                    attendee_data.get("responseStatus", "none")
                ),
                is_organiser=attendee_data.get("organizer", False),
                data_payload=attendee_data,
            )
            self.db.add(attendee)
        await self.db.flush()

    @staticmethod
    def _extract_external_id(event_data: dict[str, Any], provider: str) -> str | None:
        """Extract external ID from event data."""
        if provider == "google":
            return event_data.get("id") or event_data.get("eventId")
        elif provider == "microsoft":
            return event_data.get("id") or event_data.get("iCalUId")
        return event_data.get("id")

    @classmethod
    def _extract_event_fields(cls, event_data: dict[str, Any], provider: str) -> dict[str, Any]:
        """Extract event fields from provider data."""
        if provider == "google":
            return cls._extract_google_event_fields(event_data)
        if provider == "microsoft":
            return cls._extract_microsoft_event_fields(event_data)
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
        for entry in conference_data.get("entryPoints", []):
            if entry.get("entryPointType") == "video":
                fields["conference_link"] = entry.get("uri")
                break

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
        fields = {
            "title": event_data.get("subject") or DEFAULT_EVENT_TITLE,
            "description": event_data.get("bodyPreview")
            or event_data.get("body", {}).get("content"),
            "location": event_data.get("location", {}).get("displayName"),
            "status": event_data.get("status"),
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
        if isinstance(online_meeting, dict):
            fields["conference_link"] = online_meeting.get("joinUrl")

    @classmethod
    def _apply_microsoft_source_modified(
        cls, fields: dict[str, Any], event_data: dict[str, Any]
    ) -> None:
        modified = event_data.get("lastModifiedDateTime")
        if modified:
            fields["source_modified_at"] = cls._parse_iso_datetime(modified)

    @staticmethod
    def _extract_generic_event_fields(event_data: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": event_data.get("summary") or event_data.get("subject") or DEFAULT_EVENT_TITLE,
            "description": event_data.get("description"),
            "location": event_data.get("location"),
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
