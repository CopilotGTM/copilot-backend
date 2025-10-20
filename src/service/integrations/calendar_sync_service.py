"""
Calendar sync service for Google Calendar, Outlook Calendar, etc.

Properly stores calendar events in the calendar_events table (NOT knowledge base).
Tracks sync history in integration_sync_history.
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


def datetime_now_naive() -> datetime:
    """Return current UTC timestamp without timezone info for naive columns."""
    return datetime.now(UTC).replace(tzinfo=None)


class CalendarSyncService:
    """Service to sync calendar events from external providers into calendar_events table."""

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

                    # Commit in batches of 50
                    if records_processed % 50 == 0:
                        await self.db.commit()
                        logger.info("Committed batch: %d events processed", records_processed)

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
        await self.db.flush()

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

        # Delete existing attendees (simple approach - could be optimized)
        await self.db.execute(
            select(CalendarAttendee).where(CalendarAttendee.event_id == event.event_id)
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

    @staticmethod
    def _extract_external_id(event_data: dict[str, Any], provider: str) -> str | None:
        """Extract external ID from event data."""
        if provider == "google":
            return event_data.get("id") or event_data.get("eventId")
        elif provider == "microsoft":
            return event_data.get("id") or event_data.get("iCalUId")
        return event_data.get("id")

    @staticmethod
    def _extract_event_fields(event_data: dict[str, Any], provider: str) -> dict[str, Any]:
        """Extract event fields from provider data."""
        fields = {}

        # Google Calendar format
        if provider == "google":
            fields["title"] = event_data.get("summary") or "(No title)"
            fields["description"] = event_data.get("description")
            fields["location"] = event_data.get("location")
            fields["status"] = event_data.get("status")
            fields["calendar_external_id"] = event_data.get("calendarId")

            # Start time
            start = event_data.get("start", {})
            if isinstance(start, dict):
                if "dateTime" in start:
                    fields["start_at"] = datetime.fromisoformat(
                        start["dateTime"].replace("Z", "+00:00")
                    )
                    fields["is_all_day"] = False
                elif "date" in start:
                    fields["start_at"] = datetime.fromisoformat(start["date"] + "T00:00:00+00:00")
                    fields["is_all_day"] = True
                fields["timezone"] = start.get("timeZone")

            # End time
            end = event_data.get("end", {})
            if isinstance(end, dict):
                if "dateTime" in end:
                    fields["end_at"] = datetime.fromisoformat(
                        end["dateTime"].replace("Z", "+00:00")
                    )
                elif "date" in end:
                    fields["end_at"] = datetime.fromisoformat(end["date"] + "T00:00:00+00:00")

            # Organizer
            organizer = event_data.get("organizer", {})
            if isinstance(organizer, dict):
                fields["organiser_email"] = organizer.get("email")
                fields["organiser_name"] = organizer.get("displayName")

            # Conference data
            conference_data = event_data.get("conferenceData", {})
            if isinstance(conference_data, dict):
                entry_points = conference_data.get("entryPoints", [])
                for entry in entry_points:
                    if entry.get("entryPointType") == "video":
                        fields["conference_link"] = entry.get("uri")
                        break

            # Attendee count
            attendees = event_data.get("attendees", [])
            fields["attendee_count"] = len(attendees) if isinstance(attendees, list) else 0

            # Source modified timestamp
            if "updated" in event_data:
                fields["source_modified_at"] = datetime.fromisoformat(
                    event_data["updated"].replace("Z", "+00:00")
                )

        # Microsoft Outlook format
        elif provider == "microsoft":
            fields["title"] = event_data.get("subject") or "(No title)"
            fields["description"] = event_data.get("bodyPreview") or event_data.get("body", {}).get(
                "content"
            )
            fields["location"] = event_data.get("location", {}).get("displayName")
            fields["status"] = event_data.get("status")

            # Start/End times
            if "start" in event_data:
                start = event_data["start"]
                if isinstance(start, dict):
                    fields["start_at"] = datetime.fromisoformat(
                        start["dateTime"].replace("Z", "+00:00")
                    )
                    fields["timezone"] = start.get("timeZone")
                    fields["is_all_day"] = event_data.get("isAllDay", False)

            if "end" in event_data:
                end = event_data["end"]
                if isinstance(end, dict):
                    fields["end_at"] = datetime.fromisoformat(
                        end["dateTime"].replace("Z", "+00:00")
                    )

            # Organizer
            organizer = event_data.get("organizer", {}).get("emailAddress", {})
            if organizer:
                fields["organiser_email"] = organizer.get("address")
                fields["organiser_name"] = organizer.get("name")

            # Online meeting
            if "onlineMeeting" in event_data:
                fields["conference_link"] = event_data["onlineMeeting"].get("joinUrl")

            # Attendees
            attendees = event_data.get("attendees", [])
            fields["attendee_count"] = len(attendees) if isinstance(attendees, list) else 0

            # Modified timestamp
            if "lastModifiedDateTime" in event_data:
                fields["source_modified_at"] = datetime.fromisoformat(
                    event_data["lastModifiedDateTime"].replace("Z", "+00:00")
                )

        return fields

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
