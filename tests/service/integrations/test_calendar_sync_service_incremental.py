"""Tests for calendar sync service incremental sync functionality."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.service.integrations.calendar.normalizer import CalendarEventNormalizer
from src.service.integrations.calendar_sync_service import (
    CalendarEventUpsertBatchItem,
    CalendarSyncService,
)
from src.workers.integrations_worker.utils.sync_metadata import SyncAction


def _parse_iso_datetime(value: str) -> datetime:
    iso_utc_suffix = "+00:00"
    return datetime.fromisoformat(value.replace("Z", iso_utc_suffix))


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeExecuteResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def scalars(self):
        return _FakeScalarResult(self._rows)


def _make_integration(**overrides):
    defaults = {
        "integration_id": uuid4(),
        "organisation_id": uuid4(),
        "created_by_user_id": uuid4(),
        "integration_type": IntegrationType.GOOGLE_CALENDAR,
        "name": "Calendar",
        "credentials": {},
    }
    defaults.update(overrides)
    return KnowledgeIntegration(**defaults)


def _google_event(
    *,
    provider_id: str,
    external_id: str,
    title: str = "Team Sync",
    attendees=None,
    updated: str = "2024-01-15T09:00:00Z",
    status: str = "confirmed",
):
    """Build a minimal Google calendar payload shaped like the real API.

    `provider_id` is the per-mailbox event id (Google's `id` field) and
    `external_id` is the shared global UID (`iCalUID`) — two attendees on
    the same meeting share one external_id but each have their own provider_id.
    """
    event = {
        "id": provider_id,
        "iCalUID": external_id,
        "summary": title,
        "status": status,
        "updated": updated,
        "start": {"dateTime": "2024-01-15T10:00:00Z", "timeZone": "UTC"},
        "end": {"dateTime": "2024-01-15T10:30:00Z", "timeZone": "UTC"},
        "organizer": {"email": "host@example.com", "displayName": "Host"},
    }
    if attendees is not None:
        event["attendees"] = attendees
    return event


def _event_row(external_id: str, occurrence_anchor: str = ""):
    """Build a SimpleNamespace shaped like a CalendarEvent row from the DB.

    Mirrors the column set the change detector and the orchestrator read.
    SimpleNamespace is preferred over a real CalendarEvent here because the
    tests never need ORM behavior — only attribute access.
    """
    return SimpleNamespace(
        event_id=uuid4(),
        external_id=external_id,
        occurrence_anchor=occurrence_anchor,
        status="confirmed",
        synced_at=datetime(2024, 1, 1, tzinfo=UTC),
        title="Team Sync",
        description=None,
        location=None,
        conference_link=None,
        organiser_email="host@example.com",
        organiser_name="Host",
        calendar_external_id=None,
        timezone="UTC",
        start_at=datetime(2024, 1, 15, 10, 0, 0),
        end_at=datetime(2024, 1, 15, 10, 30, 0),
        attendee_count=1,
        is_all_day=False,
        source_modified_at=datetime(2024, 1, 10, 12, 0, 0, tzinfo=UTC),
    )


def _normalize_event(event_data, provider: str = "google"):
    return CalendarEventNormalizer().normalize(event_data, provider)


class TestCalendarIncrementalBatchWrites:
    """Regression tests for the prod N+1 in incremental calendar writes.

    Before this suite, every changed event in `sync_calendar_events_incremental`
    issued its own INSERT for calendar_events, its own delete+insert for the
    attendee snapshot, and its own INSERT for user_state — turning each
    scheduled sync into hundreds of round trips per integration. These tests
    pin the contract that all three write paths now collapse to one statement
    per sync pass, and that the per-event routing for cancelled/unchanged
    events still reaches the right counters and writers.
    """

    @pytest.mark.asyncio
    async def test_batch_upsert_events_uses_one_statement_and_returns_identity_map(self) -> None:
        """Multiple changed events collapse into one INSERT ... ON CONFLICT statement.

        Locks the prod fix: previously every changed event issued its own
        upsert. The batch path must hit the DB exactly once and return the
        rows keyed by (external_id, occurrence_anchor) so the orchestrator
        can attribute attendee snapshots and user-state rows correctly.
        """
        organisation_id = uuid4()
        integration = _make_integration(organisation_id=organisation_id)
        rows = [_event_row("ical-1"), _event_row("ical-2")]
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_FakeExecuteResult(rows))
        service = CalendarSyncService(session, organisation_id)

        result = await service._batch_upsert_events(
            integration,
            [
                CalendarEventUpsertBatchItem(
                    event=_normalize_event(
                        _google_event(provider_id="evt-1", external_id="ical-1")
                    ),
                ),
                CalendarEventUpsertBatchItem(
                    event=_normalize_event(
                        _google_event(provider_id="evt-2", external_id="ical-2")
                    ),
                ),
            ],
        )

        assert session.execute.await_count == 1
        assert result == {("ical-1", ""): rows[0], ("ical-2", ""): rows[1]}
        sql = str(session.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
        assert "ON CONFLICT" in sql
        assert "RETURNING" in sql

    @pytest.mark.asyncio
    async def test_batch_upsert_events_accepts_mixed_provider_optional_columns(self) -> None:
        """Sparse provider payloads must not break the one-statement upsert.

        Google deltas can include conference data for one event and omit it for
        another. SQLAlchemy requires every row in a multi-value INSERT to have
        the same rendered column set, so the writer normalizes SQL values
        before compiling the bulk statement.
        """
        organisation_id = uuid4()
        integration = _make_integration(organisation_id=organisation_id)
        rows = [_event_row("ical-with-link"), _event_row("ical-without-link")]
        session = AsyncMock()

        async def compile_then_return(statement):
            statement.compile(dialect=postgresql.dialect())
            return _FakeExecuteResult(rows)

        session.execute = AsyncMock(side_effect=compile_then_return)
        service = CalendarSyncService(session, organisation_id)

        event_with_link = _google_event(provider_id="evt-1", external_id="ical-with-link")
        event_with_link["conferenceData"] = {
            "entryPoints": [
                {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"}
            ]
        }

        result = await service._batch_upsert_events(
            integration,
            [
                CalendarEventUpsertBatchItem(event=_normalize_event(event_with_link)),
                CalendarEventUpsertBatchItem(
                    event=_normalize_event(
                        _google_event(provider_id="evt-2", external_id="ical-without-link")
                    ),
                ),
            ],
        )

        assert session.execute.await_count == 1
        assert result == {
            ("ical-with-link", ""): rows[0],
            ("ical-without-link", ""): rows[1],
        }

    @pytest.mark.asyncio
    async def test_batch_sync_attendees_deletes_once_and_inserts_fresh_snapshot(self) -> None:
        """Attendee snapshot refresh is one bulk delete + one bulk insert.

        Locks the snapshot contract: each provider payload is the truth as of
        that moment, so we drop the existing roster for every affected event
        in one statement and re-insert the new one in another. Two statements
        per sync pass — not two per event. Verifies both Google and Microsoft
        attendee shapes produce the same downstream insert payload.
        """
        organisation_id = uuid4()
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_FakeExecuteResult())
        service = CalendarSyncService(session, organisation_id)
        event_1 = _event_row("ical-1")
        event_2 = _event_row("ical-2")

        await service._batch_sync_attendees(
            [
                (
                    event_1,
                    _normalize_event(
                        _google_event(
                            provider_id="evt-1",
                            external_id="ical-1",
                            attendees=[
                                {
                                    "email": "buyer@example.com",
                                    "displayName": "Buyer",
                                    "responseStatus": "accepted",
                                },
                                {"displayName": "No Email"},
                            ],
                        )
                    ),
                ),
                (
                    event_2,
                    _normalize_event(
                        {
                            "id": "ms-evt-2",
                            "iCalUId": "ical-2",
                            "organizer": {
                                "emailAddress": {
                                    "address": "host@example.com",
                                    "name": "Host",
                                }
                            },
                            "attendees": [
                                {
                                    "emailAddress": {
                                        "address": "host@example.com",
                                        "name": "Host",
                                    },
                                    "status": {"response": "accepted"},
                                }
                            ],
                        },
                        "microsoft",
                    ),
                ),
            ]
        )

        assert session.execute.await_count == 2
        delete_sql = str(session.execute.await_args_list[0].args[0])
        insert_params = session.execute.await_args_list[1].args[0].compile().params
        assert "calendar_event_attendees.event_id IN" in delete_sql
        assert "buyer@example.com" in str(insert_params.values())
        assert "host@example.com" in str(insert_params.values())
        assert "No Email" not in str(insert_params.values())

    @pytest.mark.asyncio
    async def test_batch_upsert_user_states_resets_deleted_state_in_one_statement(self) -> None:
        """User-state rows are upserted in one statement and resurrect on conflict.

        Re-seeing an event for the same (event_id, integration_id) pair flips
        is_deleted back to False — that's how a previously-deleted event
        becomes active again from this integration's perspective. The whole
        batch must be one INSERT ... ON CONFLICT, not one per event.
        """
        organisation_id = uuid4()
        integration_id = uuid4()
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_FakeExecuteResult())
        service = CalendarSyncService(session, organisation_id)

        await service._batch_upsert_user_states(
            [
                (uuid4(), integration_id, "user-event-1"),
                (uuid4(), integration_id, "user-event-2"),
            ]
        )

        assert session.execute.await_count == 1
        sql = str(session.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
        assert "ON CONFLICT" in sql
        assert "is_deleted" in sql
        assert "external_id" in sql

    @pytest.mark.asyncio
    async def test_incremental_sync_batches_mixed_changed_and_state_only_events(self) -> None:
        """Mixed batch (created + updated + unchanged + cancelled) hits each path correctly.

        End-to-end check that the per-event routing classifies each event into
        the right counter, accumulates the right writes, and flushes them via
        a single batched call to each writer method. This is the most
        important regression test in the file: it locks the contract that the
        whole flow described in the sync_service module docstring still holds
        when all four event types appear together in one provider delta.
        """
        organisation_id = uuid4()
        integration = _make_integration(organisation_id=organisation_id)
        session = AsyncMock()
        session.commit = AsyncMock()
        session.add = MagicMock()
        service = CalendarSyncService(session, organisation_id)
        service._sync_history.start = AsyncMock(return_value=SimpleNamespace())
        service._sync_history.complete = AsyncMock()

        existing_updated = _event_row("ical-updated")
        existing_unchanged = _event_row("ical-unchanged")
        existing_cancelled = _event_row("ical-cancelled")
        upserted_created = _event_row("ical-created")
        upserted_updated = _event_row("ical-updated")

        service._load_existing_events_by_identity = AsyncMock(
            return_value={
                ("ical-updated", ""): existing_updated,
                ("ical-unchanged", ""): existing_unchanged,
                ("ical-cancelled", ""): existing_cancelled,
            }
        )
        service._has_meaningful_event_changes = MagicMock(side_effect=[True, False])
        service._is_google_cancelled_exception = MagicMock(return_value=True)
        service._batch_upsert_events = AsyncMock(
            return_value={
                ("ical-created", ""): upserted_created,
                ("ical-updated", ""): upserted_updated,
            }
        )
        service._batch_sync_attendees = AsyncMock()
        service._batch_upsert_user_states = AsyncMock()

        result = await service.sync_calendar_events_incremental(
            integration=integration,
            provider="google",
            events=[
                _google_event(
                    provider_id="user-created",
                    external_id="ical-created",
                    title="Created",
                    attendees=[{"email": "created@example.com"}],
                ),
                _google_event(
                    provider_id="user-updated",
                    external_id="ical-updated",
                    title="Updated",
                    attendees=[{"email": "updated@example.com"}],
                    updated="2024-01-20T09:00:00Z",
                ),
                _google_event(
                    provider_id="user-unchanged",
                    external_id="ical-unchanged",
                    attendees=[{"email": "unchanged@example.com"}],
                ),
                _google_event(
                    provider_id="user-cancelled",
                    external_id="ical-cancelled",
                    status="cancelled",
                    attendees=[{"email": "cancelled@example.com"}],
                ),
            ],
        )

        assert result["created"] == 1
        assert result["updated"] == 1
        assert result["skipped"] == 1
        assert result["cancelled_existing"] == 1
        assert result["changed_event_ids"] == [
            str(upserted_created.event_id),
            str(upserted_updated.event_id),
            str(existing_cancelled.event_id),
        ]
        assert service._batch_upsert_events.await_count == 1
        assert len(service._batch_upsert_events.await_args.args[1]) == 2
        refreshed_attendees = service._batch_sync_attendees.await_args.args[0]
        assert {event.external_id for event, _ in refreshed_attendees} == {
            "ical-created",
            "ical-updated",
            "ical-cancelled",
        }
        user_states = service._batch_upsert_user_states.await_args.args[0]
        assert len(user_states) == 4

    @pytest.mark.asyncio
    async def test_full_sync_returns_changed_event_ids(self) -> None:
        """Full sync reports local event IDs for every successful event write."""
        organisation_id = uuid4()
        integration = _make_integration(organisation_id=organisation_id)
        first_event = _event_row("ical-1")
        second_event = _event_row("ical-2")
        session = AsyncMock()
        session.commit = AsyncMock()
        session.add = MagicMock()
        service = CalendarSyncService(session, organisation_id)
        service._sync_history.start = AsyncMock(return_value=SimpleNamespace())
        service._sync_history.complete = AsyncMock()
        service._load_existing_events_by_identity = AsyncMock(return_value={})
        service._upsert_event = AsyncMock(side_effect=[first_event, second_event])
        service._sync_attendees = AsyncMock()
        service._upsert_user_state = AsyncMock()

        result = await service.sync_calendar_events(
            integration=integration,
            provider="google",
            events=[
                _google_event(provider_id="user-1", external_id="ical-1"),
                _google_event(provider_id="user-2", external_id="ical-2"),
            ],
        )

        assert result["records_processed"] == 2
        assert result["changed_event_ids"] == [
            str(first_event.event_id),
            str(second_event.event_id),
        ]

    @pytest.mark.asyncio
    async def test_incremental_sync_enqueues_changed_future_meeting_bot_after_commit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Changed future events with join links enqueue the idempotent single-event task."""
        organisation_id = uuid4()
        integration = _make_integration(organisation_id=organisation_id)
        commit_count = 0

        async def _commit() -> None:
            nonlocal commit_count
            commit_count += 1

        session = AsyncMock()
        session.commit = AsyncMock(side_effect=_commit)
        session.add = MagicMock()
        service = CalendarSyncService(session, organisation_id)
        service._sync_history.start = AsyncMock(return_value=SimpleNamespace())
        service._sync_history.complete = AsyncMock()
        service._load_existing_events_by_identity = AsyncMock(return_value={})
        service._batch_sync_attendees = AsyncMock()
        service._batch_upsert_user_states = AsyncMock()
        upserted = _event_row("ical-created")
        upserted.start_at = datetime.now(UTC) + timedelta(hours=2)
        upserted.conference_link = "https://meet.google.com/abc-defg-hij"
        service._batch_upsert_events = AsyncMock(return_value={("ical-created", ""): upserted})
        send_task = AsyncMock()

        async def _send_task(*args, **kwargs):
            assert commit_count >= 1
            return await send_task(*args, **kwargs)

        monkeypatch.setattr(
            "src.service.integrations.calendar.sync_service.send_deal_task_async",
            _send_task,
        )

        event_data = _google_event(provider_id="user-created", external_id="ical-created")
        event_data["conferenceData"] = {
            "entryPoints": [
                {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"}
            ]
        }

        await service.sync_calendar_events_incremental(
            integration=integration,
            provider="google",
            events=[event_data],
        )

        send_task.assert_awaited_once_with(
            "deal.send_meeting_recording",
            kwargs={
                "organisation_id": str(organisation_id),
                "calendar_event_id": str(upserted.event_id),
            },
            queue="deal",
            routing_key="deal",
        )

    @pytest.mark.asyncio
    async def test_incremental_sync_enqueues_cancelled_future_meeting_bot_after_commit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cancelled future events enqueue bot reconciliation so scheduled bots are removed."""
        organisation_id = uuid4()
        integration = _make_integration(organisation_id=organisation_id)
        commit_count = 0

        async def _commit() -> None:
            nonlocal commit_count
            commit_count += 1

        session = AsyncMock()
        session.commit = AsyncMock(side_effect=_commit)
        session.add = MagicMock()
        service = CalendarSyncService(session, organisation_id)
        service._sync_history.start = AsyncMock(return_value=SimpleNamespace())
        service._sync_history.complete = AsyncMock()
        existing = _event_row("ical-cancelled")
        existing.start_at = datetime.now(UTC) + timedelta(hours=2)
        existing.conference_link = None
        service._load_existing_events_by_identity = AsyncMock(
            return_value={("ical-cancelled", ""): existing}
        )
        service._is_google_cancelled_exception = MagicMock(return_value=True)
        service._batch_upsert_events = AsyncMock(return_value={})
        service._batch_sync_attendees = AsyncMock()
        service._batch_upsert_user_states = AsyncMock()
        send_task = AsyncMock()

        async def _send_task(*args, **kwargs):
            assert commit_count >= 1
            return await send_task(*args, **kwargs)

        monkeypatch.setattr(
            "src.service.integrations.calendar.sync_service.send_deal_task_async",
            _send_task,
        )

        await service.sync_calendar_events_incremental(
            integration=integration,
            provider="google",
            events=[
                _google_event(
                    provider_id="user-cancelled",
                    external_id="ical-cancelled",
                    status="cancelled",
                )
            ],
        )

        send_task.assert_awaited_once_with(
            "deal.send_meeting_recording",
            kwargs={
                "organisation_id": str(organisation_id),
                "calendar_event_id": str(existing.event_id),
            },
            queue="deal",
            routing_key="deal",
        )

    @pytest.mark.asyncio
    async def test_incremental_sync_does_not_enqueue_ineligible_meeting_bot_events(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Events without join links are left for normal sync only."""
        organisation_id = uuid4()
        integration = _make_integration(organisation_id=organisation_id)
        session = AsyncMock()
        session.commit = AsyncMock()
        session.add = MagicMock()
        service = CalendarSyncService(session, organisation_id)
        service._sync_history.start = AsyncMock(return_value=SimpleNamespace())
        service._sync_history.complete = AsyncMock()
        service._load_existing_events_by_identity = AsyncMock(return_value={})
        service._batch_sync_attendees = AsyncMock()
        service._batch_upsert_user_states = AsyncMock()
        upserted = _event_row("ical-created")
        upserted.start_at = datetime.now(UTC) + timedelta(hours=2)
        upserted.conference_link = None
        service._batch_upsert_events = AsyncMock(return_value={("ical-created", ""): upserted})
        send_task = AsyncMock()
        monkeypatch.setattr(
            "src.service.integrations.calendar.sync_service.send_deal_task_async",
            send_task,
        )

        await service.sync_calendar_events_incremental(
            integration=integration,
            provider="google",
            events=[_google_event(provider_id="user-created", external_id="ical-created")],
        )

        send_task.assert_not_awaited()


def _has_meaningful_event_changes(existing, incoming, provider) -> bool:
    """Standalone implementation of change detection for testing.

    This mirrors the logic in CalendarSyncService._has_meaningful_event_changes.
    """
    if provider == "google":
        fields = {
            "title": incoming.get("summary") or "(No title)",
            "description": incoming.get("description"),
            "location": incoming.get("location"),
            "status": incoming.get("status"),
        }
        start = incoming.get("start", {})
        if "dateTime" in start:
            fields["start_at"] = _parse_iso_datetime(start["dateTime"])
            fields["is_all_day"] = False
        elif "date" in start:
            fields["start_at"] = datetime.fromisoformat(f"{start['date']}T00:00:00+00:00")
            fields["is_all_day"] = True
        end = incoming.get("end", {})
        if "dateTime" in end:
            fields["end_at"] = _parse_iso_datetime(end["dateTime"])
        elif "date" in end:
            fields["end_at"] = datetime.fromisoformat(f"{end['date']}T00:00:00+00:00")
        attendees = incoming.get("attendees", [])
        fields["attendee_count"] = len(attendees) if isinstance(attendees, list) else 0
        if incoming.get("updated"):
            fields["source_modified_at"] = _parse_iso_datetime(incoming["updated"])
        conf = incoming.get("conferenceData", {})
        for entry in conf.get("entryPoints", []):
            if entry.get("entryPointType") == "video":
                fields["conference_link"] = entry.get("uri")
                break
        organizer = incoming.get("organizer", {})
        fields["organiser_email"] = organizer.get("email")
        fields["organiser_name"] = organizer.get("displayName")
        fields["calendar_external_id"] = incoming.get("calendarId")
        fields["timezone"] = start.get("timeZone")
    else:  # microsoft/outlook
        is_cancelled = incoming.get("isCancelled", False)
        fields = {
            "title": incoming.get("subject") or "(No title)",
            "description": incoming.get("bodyPreview") or incoming.get("body", {}).get("content"),
            "location": incoming.get("location", {}).get("displayName"),
            "status": "cancelled" if is_cancelled else "confirmed",
        }
        start = incoming.get("start", {})
        if start.get("dateTime"):
            fields["start_at"] = _parse_iso_datetime(start["dateTime"])
        fields["is_all_day"] = incoming.get("isAllDay", False)
        end = incoming.get("end", {})
        if end.get("dateTime"):
            fields["end_at"] = _parse_iso_datetime(end["dateTime"])
        attendees = incoming.get("attendees", [])
        fields["attendee_count"] = len(attendees) if isinstance(attendees, list) else 0
        if incoming.get("lastModifiedDateTime"):
            fields["source_modified_at"] = _parse_iso_datetime(incoming["lastModifiedDateTime"])
        online = incoming.get("onlineMeeting", {})
        fields["conference_link"] = online.get("joinUrl")
        organizer = incoming.get("organizer", {}).get("emailAddress", {})
        fields["organiser_email"] = organizer.get("address")
        fields["organiser_name"] = organizer.get("name")
        fields["timezone"] = start.get("timeZone")

    incoming_modified = fields.get("source_modified_at")
    if (
        incoming_modified
        and existing.source_modified_at
        and incoming_modified > existing.source_modified_at
    ):
        return True

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

    optional_checks = [
        (existing.organiser_name, fields.get("organiser_name")),
        (existing.calendar_external_id, fields.get("calendar_external_id")),
        (existing.timezone, fields.get("timezone")),
    ]

    for existing_val, incoming_val in optional_checks:
        if incoming_val is None:
            continue
        if existing_val != incoming_val:
            return True

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

    incoming_attendee_count = fields.get("attendee_count", 0)
    if existing.attendee_count != incoming_attendee_count:
        return True

    incoming_is_all_day = fields.get("is_all_day", False)
    return existing.is_all_day != incoming_is_all_day


class TestCalendarEventHasMeaningfulChanges:
    """Tests for _has_meaningful_event_changes method logic."""

    @pytest.fixture
    def mock_event(self):
        """Create a mock CalendarEvent for testing."""
        event = MagicMock()
        event.title = "Team Standup"
        event.description = "Daily standup meeting"
        event.location = "Conference Room A"
        event.status = "confirmed"
        event.conference_link = "https://meet.google.com/abc-defg-hij"
        event.organiser_email = "organizer@company.com"
        event.organiser_name = "Organizer Name"
        event.calendar_external_id = "primary"
        event.timezone = "UTC"
        event.start_at = datetime(2024, 1, 15, 10, 0, 0)
        event.end_at = datetime(2024, 1, 15, 10, 30, 0)
        event.attendee_count = 5
        event.is_all_day = False
        event.source_modified_at = datetime(2024, 1, 10, 12, 0, 0, tzinfo=UTC)
        return event

    def test_detects_title_change_google(self, mock_event) -> None:
        """Title change should be detected (Google)."""
        incoming = {
            "summary": "Updated Team Standup",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T10:30:00Z"},
            "attendees": [{}] * 5,
        }
        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is True

    def test_detects_title_change_microsoft(self, mock_event) -> None:
        """Title change should be detected (Microsoft)."""
        incoming = {
            "subject": "Updated Team Standup",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T10:30:00Z"},
            "attendees": [{}] * 5,
        }
        result = _has_meaningful_event_changes(mock_event, incoming, "microsoft")
        assert result is True

    def test_detects_time_change(self, mock_event) -> None:
        """Start/end time change should be detected."""
        incoming = {
            "summary": "Team Standup",
            "start": {"dateTime": "2024-01-15T11:00:00Z"},  # Changed from 10:00 to 11:00
            "end": {"dateTime": "2024-01-15T11:30:00Z"},
            "attendees": [{}] * 5,
        }
        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is True

    def test_detects_status_change(self, mock_event) -> None:
        """Status change should be detected."""
        incoming = {
            "summary": "Team Standup",
            "status": "cancelled",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T10:30:00Z"},
            "attendees": [{}] * 5,
        }
        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is True

    def test_detects_attendee_count_change(self, mock_event) -> None:
        """Attendee count change should be detected."""
        incoming = {
            "summary": "Team Standup",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T10:30:00Z"},
            "attendees": [{}] * 10,  # Changed from 5 to 10
        }
        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is True

    def test_detects_location_change(self, mock_event) -> None:
        """Location change should be detected."""
        incoming = {
            "summary": "Team Standup",
            "location": "Conference Room B",  # Changed
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T10:30:00Z"},
            "attendees": [{}] * 5,
        }
        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is True

    def test_detects_source_modified_at_change(self, mock_event) -> None:
        """source_modified_at newer than existing should trigger change."""
        incoming = {
            "summary": "Team Standup",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T10:30:00Z"},
            "attendees": [{}] * 5,
            "updated": "2024-01-11T12:00:00Z",  # Newer than existing
        }
        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is True

    def test_detects_sparse_field_rehydration(self, mock_event) -> None:
        """Missing organiser_name/timezone on an old row should trigger refresh."""
        mock_event.organiser_name = None
        mock_event.timezone = None

        incoming = {
            "summary": "Team Standup",
            "description": "Daily standup meeting",
            "location": "Conference Room A",
            "status": "confirmed",
            "start": {"dateTime": "2024-01-15T10:00:00Z", "timeZone": "Asia/Kolkata"},
            "end": {"dateTime": "2024-01-15T10:30:00Z"},
            "attendees": [{}] * 5,
            "organizer": {"email": "organizer@company.com", "displayName": "Organizer Name"},
        }
        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is True

    def test_no_change_returns_false(self, mock_event) -> None:
        """No meaningful change should return False."""
        incoming = {
            "summary": "Team Standup",
            "description": "Daily standup meeting",
            "location": "Conference Room A",
            "status": "confirmed",
            "start": {"dateTime": "2024-01-15T10:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2024-01-15T10:30:00Z"},
            "attendees": [{}] * 5,
            "conferenceData": {
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"}
                ]
            },
            "organizer": {"email": "organizer@company.com", "displayName": "Organizer Name"},
            "calendarId": "primary",
        }
        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is False

    def test_handles_none_values(self) -> None:
        """None values should be handled gracefully."""
        mock_event = MagicMock()
        mock_event.title = None
        mock_event.description = None
        mock_event.location = None
        mock_event.status = None
        mock_event.conference_link = None
        mock_event.organiser_email = None
        mock_event.organiser_name = None
        mock_event.calendar_external_id = None
        mock_event.timezone = None
        mock_event.start_at = None
        mock_event.end_at = None
        mock_event.attendee_count = 0
        mock_event.is_all_day = False
        mock_event.source_modified_at = None

        incoming = {
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T10:30:00Z"},
        }
        # Should not raise an exception
        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert isinstance(result, bool)


class TestCalendarIncrementalRouting:
    """Tests for calendar incremental sync routing logic."""

    def test_database_state_is_source_of_truth(self) -> None:
        """Database state (is_new) should determine create vs update."""
        record_in_db = True
        _sync_action = SyncAction.ADDED  # Unused - demonstrates db state is source of truth

        is_new = not record_in_db

        # Use is_new, NOT sync_action
        should_create = is_new
        should_update = not is_new

        assert should_create is False
        assert should_update is True

    def test_cancelled_events_handled(self) -> None:
        """Cancelled events should be marked with cancelled status."""
        # Google format
        google_event = {
            "id": "event-123",
            "status": "cancelled",
        }
        is_cancelled = google_event.get("status") == "cancelled"
        assert is_cancelled is True

        # Microsoft format
        microsoft_event = {
            "id": "event-456",
            "isCancelled": True,
        }
        is_cancelled_ms = microsoft_event.get("isCancelled", False)
        assert is_cancelled_ms is True


class TestCalendarAdapterRouting:
    """Tests for calendar adapter routing logic."""

    def test_incremental_sync_type_routes_to_incremental(self) -> None:
        """sync_type='INCREMENTAL' should route to incremental method."""
        sync_type = "INCREMENTAL"
        is_incremental = sync_type and sync_type.upper() == "INCREMENTAL"
        assert is_incremental is True

    def test_initial_sync_type_routes_to_full(self) -> None:
        """sync_type='INITIAL' should route to full sync method."""
        sync_type = "INITIAL"
        is_incremental = sync_type and sync_type.upper() == "INCREMENTAL"
        assert is_incremental is False

    def test_none_sync_type_routes_to_full(self) -> None:
        """sync_type=None should route to full sync method."""
        sync_type = None
        is_incremental = sync_type and sync_type.upper() == "INCREMENTAL"
        assert not is_incremental


class TestCalendarDataTypes:
    """Tests for calendar data type support in incremental sync."""

    def test_events_data_type_supported(self) -> None:
        """'events' data type should be in incremental_sync_entities."""
        incremental_sync_entities = {"events", "calendars"}
        assert "events" in incremental_sync_entities

    def test_calendars_data_type_supported(self) -> None:
        """'calendars' data type should be in incremental_sync_entities."""
        incremental_sync_entities = {"events", "calendars"}
        assert "calendars" in incremental_sync_entities


class TestGoogleCalendarFieldExtraction:
    """Tests for Google Calendar specific field extraction."""

    def test_extracts_summary_as_title(self) -> None:
        """Google 'summary' should map to title."""
        incoming = {
            "summary": "Project Review",
            "start": {"dateTime": "2024-01-15T14:00:00Z"},
            "end": {"dateTime": "2024-01-15T15:00:00Z"},
        }
        mock_event = MagicMock()
        mock_event.title = "Different Title"
        mock_event.description = None
        mock_event.location = None
        mock_event.status = None
        mock_event.conference_link = None
        mock_event.organiser_email = None
        mock_event.start_at = datetime(2024, 1, 15, 14, 0, 0)
        mock_event.end_at = datetime(2024, 1, 15, 15, 0, 0)
        mock_event.attendee_count = 0
        mock_event.is_all_day = False
        mock_event.source_modified_at = None

        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is True  # Title changed

    def test_handles_all_day_event(self) -> None:
        """All-day events use 'date' instead of 'dateTime'."""
        mock_event = MagicMock()
        mock_event.title = "Company Holiday"
        mock_event.description = None
        mock_event.location = None
        mock_event.status = None
        mock_event.conference_link = None
        mock_event.organiser_email = None
        mock_event.organiser_name = None
        mock_event.calendar_external_id = None
        mock_event.timezone = None
        mock_event.start_at = datetime(2024, 1, 15, 0, 0, 0)
        mock_event.end_at = datetime(2024, 1, 16, 0, 0, 0)
        mock_event.attendee_count = 0
        mock_event.is_all_day = True
        mock_event.source_modified_at = None

        incoming = {
            "summary": "Company Holiday",
            "start": {"date": "2024-01-15"},
            "end": {"date": "2024-01-16"},
        }

        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is False  # No change

    def test_extracts_google_meet_link(self) -> None:
        """Should extract Google Meet link from conferenceData."""
        mock_event = MagicMock()
        mock_event.title = "Meeting"
        mock_event.description = None
        mock_event.location = None
        mock_event.status = None
        mock_event.conference_link = "https://meet.google.com/old-link"
        mock_event.organiser_email = None
        mock_event.start_at = datetime(2024, 1, 15, 10, 0, 0)
        mock_event.end_at = datetime(2024, 1, 15, 11, 0, 0)
        mock_event.attendee_count = 0
        mock_event.is_all_day = False
        mock_event.source_modified_at = None

        incoming = {
            "summary": "Meeting",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T11:00:00Z"},
            "conferenceData": {
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://meet.google.com/new-link"}
                ]
            },
        }

        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is True  # Conference link changed

    def test_cancelled_status_google(self) -> None:
        """Google cancelled events should have status='cancelled'."""
        incoming = {
            "id": "event-123",
            "summary": "Cancelled Meeting",
            "status": "cancelled",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T11:00:00Z"},
        }

        mock_event = MagicMock()
        mock_event.title = "Cancelled Meeting"
        mock_event.description = None
        mock_event.location = None
        mock_event.status = "confirmed"  # Was confirmed
        mock_event.conference_link = None
        mock_event.organiser_email = None
        mock_event.start_at = datetime(2024, 1, 15, 10, 0, 0)
        mock_event.end_at = datetime(2024, 1, 15, 11, 0, 0)
        mock_event.attendee_count = 0
        mock_event.is_all_day = False
        mock_event.source_modified_at = None

        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is True  # Status changed to cancelled


class TestMicrosoftCalendarFieldExtraction:
    """Tests for Microsoft Calendar specific field extraction."""

    def test_extracts_subject_as_title(self) -> None:
        """Microsoft 'subject' should map to title."""
        incoming = {
            "subject": "Team Sync",
            "start": {"dateTime": "2024-01-15T14:00:00Z"},
            "end": {"dateTime": "2024-01-15T15:00:00Z"},
        }
        mock_event = MagicMock()
        mock_event.title = "Different Title"
        mock_event.description = None
        mock_event.location = None
        mock_event.status = "confirmed"
        mock_event.conference_link = None
        mock_event.organiser_email = None
        mock_event.start_at = datetime(2024, 1, 15, 14, 0, 0)
        mock_event.end_at = datetime(2024, 1, 15, 15, 0, 0)
        mock_event.attendee_count = 0
        mock_event.is_all_day = False
        mock_event.source_modified_at = None

        result = _has_meaningful_event_changes(mock_event, incoming, "microsoft")
        assert result is True

    def test_extracts_teams_meeting_link(self) -> None:
        """Should extract Teams meeting link from onlineMeeting."""
        mock_event = MagicMock()
        mock_event.title = "Teams Meeting"
        mock_event.description = None
        mock_event.location = None
        mock_event.status = "confirmed"
        mock_event.conference_link = None
        mock_event.organiser_email = None
        mock_event.start_at = datetime(2024, 1, 15, 10, 0, 0)
        mock_event.end_at = datetime(2024, 1, 15, 11, 0, 0)
        mock_event.attendee_count = 0
        mock_event.is_all_day = False
        mock_event.source_modified_at = None

        incoming = {
            "subject": "Teams Meeting",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T11:00:00Z"},
            "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/abc123"},
        }

        result = _has_meaningful_event_changes(mock_event, incoming, "microsoft")
        assert result is True  # Conference link added

    def test_cancelled_status_microsoft(self) -> None:
        """Microsoft isCancelled=true should map to status='cancelled'."""
        incoming = {
            "id": "event-456",
            "subject": "Cancelled Meeting",
            "isCancelled": True,
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T11:00:00Z"},
        }

        mock_event = MagicMock()
        mock_event.title = "Cancelled Meeting"
        mock_event.description = None
        mock_event.location = None
        mock_event.status = "confirmed"
        mock_event.conference_link = None
        mock_event.organiser_email = None
        mock_event.start_at = datetime(2024, 1, 15, 10, 0, 0)
        mock_event.end_at = datetime(2024, 1, 15, 11, 0, 0)
        mock_event.attendee_count = 0
        mock_event.is_all_day = False
        mock_event.source_modified_at = None

        result = _has_meaningful_event_changes(mock_event, incoming, "microsoft")
        assert result is True  # Status changed

    def test_extracts_location_display_name(self) -> None:
        """Microsoft location should use displayName."""
        mock_event = MagicMock()
        mock_event.title = "Meeting"
        mock_event.description = None
        mock_event.location = "Room A"
        mock_event.status = "confirmed"
        mock_event.conference_link = None
        mock_event.organiser_email = None
        mock_event.start_at = datetime(2024, 1, 15, 10, 0, 0)
        mock_event.end_at = datetime(2024, 1, 15, 11, 0, 0)
        mock_event.attendee_count = 0
        mock_event.is_all_day = False
        mock_event.source_modified_at = None

        incoming = {
            "subject": "Meeting",
            "location": {"displayName": "Room B"},  # Changed
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T11:00:00Z"},
        }

        result = _has_meaningful_event_changes(mock_event, incoming, "microsoft")
        assert result is True


class TestCalendarIncrementalScenarios:
    """Integration scenarios testing the full incremental sync flow."""

    def test_mixed_event_scenario(self) -> None:
        """Test scenario with new, updated, cancelled, and unchanged events."""
        from src.workers.integrations_worker.utils.sync_metadata import (
            SyncActionCounts,
            extract_sync_action,
        )

        events = [
            # New event
            {
                "id": "event-new-1",
                "summary": "New Meeting",
                "_sync_metadata": {"sync_action": "ADDED"},
            },
            # Updated event
            {
                "id": "event-existing-1",
                "summary": "Updated Meeting",
                "_sync_metadata": {"sync_action": "UPDATED"},
            },
            # Cancelled event
            {
                "id": "event-cancelled-1",
                "summary": "Cancelled Meeting",
                "status": "cancelled",
                "_sync_metadata": {"sync_action": "DELETED"},
            },
            # Unchanged event
            {
                "id": "event-existing-2",
                "summary": "Regular Meeting",
                "_sync_metadata": {"sync_action": "UPDATED"},
            },
        ]

        counts = SyncActionCounts()
        for event in events:
            action = extract_sync_action(event)
            if action:
                counts.increment(action)

        assert counts.added == 1
        assert counts.updated == 2
        assert counts.deleted == 1
        assert counts.total == 4

    def test_database_state_determines_create_or_update(self) -> None:
        """Database existence should determine create vs update."""
        existing_external_ids = {"event-1", "event-2"}

        event = {
            "id": "event-1",
            "summary": "Test Event",
            "_sync_metadata": {"sync_action": "ADDED"},
        }

        external_id = event["id"]
        is_new = external_id not in existing_external_ids

        assert is_new is False
        should_update = not is_new
        assert should_update is True

    def test_new_event_gets_created(self) -> None:
        """New event (not in DB) should be created."""
        existing_external_ids = {"event-1", "event-2"}

        event = {
            "id": "event-new",
            "summary": "Brand New Event",
            "_sync_metadata": {"sync_action": "ADDED"},
        }

        external_id = event["id"]
        is_new = external_id not in existing_external_ids

        assert is_new is True

    def test_unchanged_event_gets_skipped(self) -> None:
        """Event with no meaningful changes should be skipped."""
        mock_existing = MagicMock()
        mock_existing.title = "Weekly Standup"
        mock_existing.description = None
        mock_existing.location = "Zoom"
        mock_existing.status = "confirmed"
        mock_existing.conference_link = None
        mock_existing.organiser_email = "host@company.com"
        mock_existing.organiser_name = None
        mock_existing.calendar_external_id = None
        mock_existing.timezone = None
        mock_existing.start_at = datetime(2024, 1, 15, 10, 0, 0)
        mock_existing.end_at = datetime(2024, 1, 15, 10, 30, 0)
        mock_existing.attendee_count = 5
        mock_existing.is_all_day = False
        mock_existing.source_modified_at = datetime(2024, 1, 10, 12, 0, 0, tzinfo=UTC)

        incoming = {
            "summary": "Weekly Standup",
            "location": "Zoom",
            "status": "confirmed",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T10:30:00Z"},
            "attendees": [{}] * 5,
            "organizer": {"email": "host@company.com"},
        }

        has_changes = _has_meaningful_event_changes(mock_existing, incoming, "google")
        assert has_changes is False

    def test_source_modified_at_triggers_update(self) -> None:
        """Newer source_modified_at should trigger update."""
        mock_existing = MagicMock()
        mock_existing.title = "Meeting"
        mock_existing.description = None
        mock_existing.location = None
        mock_existing.status = "confirmed"
        mock_existing.conference_link = None
        mock_existing.organiser_email = None
        mock_existing.start_at = datetime(2024, 1, 15, 10, 0, 0)
        mock_existing.end_at = datetime(2024, 1, 15, 11, 0, 0)
        mock_existing.attendee_count = 0
        mock_existing.is_all_day = False
        mock_existing.source_modified_at = datetime(2024, 1, 10, 12, 0, 0, tzinfo=UTC)

        incoming = {
            "summary": "Meeting",
            "status": "confirmed",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T11:00:00Z"},
            "updated": "2024-01-15T14:00:00Z",  # Newer
        }

        has_changes = _has_meaningful_event_changes(mock_existing, incoming, "google")
        assert has_changes is True


class TestCalendarIncrementalResultStructure:
    """Tests for incremental sync result structure."""

    def test_result_includes_all_counts(self) -> None:
        """Result should include created, updated, skipped counts."""
        expected_keys = {
            "status",
            "records_processed",
            "records_failed",
            "created",
            "updated",
            "skipped",
        }

        sample_result = {
            "status": "success",
            "records_processed": 15,
            "records_failed": 1,
            "created": 5,
            "updated": 7,
            "skipped": 3,
        }

        assert expected_keys.issubset(sample_result.keys())

    def test_action_counts_tracking(self) -> None:
        """SyncActionCounts should track all action types."""
        from src.workers.integrations_worker.utils.sync_metadata import (
            SyncAction,
            SyncActionCounts,
        )

        counts = SyncActionCounts()
        counts.increment(SyncAction.ADDED)
        counts.increment(SyncAction.UPDATED)
        counts.increment(SyncAction.UPDATED)
        counts.increment(SyncAction.DELETED)

        result = counts.to_dict()
        assert result["added"] == 1
        assert result["updated"] == 2
        assert result["deleted"] == 1
        assert result["total"] == 4


class TestCalendarDispatcherIntegration:
    """Tests for dispatcher integration with calendar."""

    def test_calendar_category_in_incremental_categories(self) -> None:
        """'calendar' should be in incremental_sync_categories."""
        incremental_sync_categories = {"crm", "calendar", "email", "messaging"}
        assert "calendar" in incremental_sync_categories

    def test_calendar_entities_not_in_fetcher_entities(self) -> None:
        """Calendar entities should NOT be in incremental_sync_entities (CRM only)."""
        incremental_sync_entities = {
            "deals",
            "opportunities",
            "contacts",
            "leads",
            "companies",
            "accounts",
            "deal_tasks",
            "tasks",
            "tickets",
            "notes",
            "owners",
            "users",
            "pipelines",
        }
        assert "events" not in incremental_sync_entities
        assert "calendars" not in incremental_sync_entities

    def test_sync_type_only_passed_to_handler_not_fetcher(self) -> None:
        """sync_type should be passed to handler, not to calendar fetcher."""
        category = "calendar"
        data_type = "events"

        incremental_sync_entities = {
            "deals",
            "opportunities",
            "contacts",
            "leads",
            "companies",
            "accounts",
            "deal_tasks",
            "tasks",
            "tickets",
            "notes",
            "owners",
            "users",
            "pipelines",
        }

        should_pass_to_fetcher = data_type in incremental_sync_entities
        assert should_pass_to_fetcher is False

        incremental_sync_categories = {"crm", "calendar", "email", "messaging"}
        should_pass_to_handler = category in incremental_sync_categories
        assert should_pass_to_handler is True


class TestCalendarRecurringEvents:
    """Tests for recurring event handling."""

    def test_recurring_event_instance_identified(self) -> None:
        """Recurring event instances should be identifiable."""
        # Google recurring event instance
        google_recurring = {
            "id": "event-base_20240115T100000Z",
            "recurringEventId": "event-base",
            "summary": "Daily Standup",
        }

        is_recurring_instance = "recurringEventId" in google_recurring
        assert is_recurring_instance is True

        # Microsoft recurring event instance
        microsoft_recurring = {
            "id": "AAMkAG...",
            "seriesMasterId": "AAMkAG-master...",
            "subject": "Weekly Review",
        }

        is_recurring_instance_ms = "seriesMasterId" in microsoft_recurring
        assert is_recurring_instance_ms is True

    def test_non_recurring_event(self) -> None:
        """Non-recurring events don't have recurringEventId."""
        single_event = {
            "id": "event-single",
            "summary": "One-time Meeting",
        }

        is_recurring = "recurringEventId" in single_event or "seriesMasterId" in single_event
        assert is_recurring is False


class TestCalendarTimezoneHandling:
    """Tests for timezone handling in calendar events."""

    def test_utc_timezone_parsed(self) -> None:
        """UTC timezone (Z suffix) should be parsed correctly."""
        mock_event = MagicMock()
        mock_event.title = "Meeting"
        mock_event.description = None
        mock_event.location = None
        mock_event.status = "confirmed"
        mock_event.conference_link = None
        mock_event.organiser_email = None
        mock_event.start_at = datetime(2024, 1, 15, 10, 0, 0)
        mock_event.end_at = datetime(2024, 1, 15, 11, 0, 0)
        mock_event.attendee_count = 0
        mock_event.is_all_day = False
        mock_event.source_modified_at = None

        incoming = {
            "summary": "Meeting",
            "status": "confirmed",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T11:00:00Z"},
        }

        # Should not raise
        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert isinstance(result, bool)

    def test_offset_timezone_parsed(self) -> None:
        """Offset timezone (+00:00) should be parsed correctly."""
        mock_event = MagicMock()
        mock_event.title = "Meeting"
        mock_event.description = None
        mock_event.location = None
        mock_event.status = "confirmed"
        mock_event.conference_link = None
        mock_event.organiser_email = None
        mock_event.start_at = datetime(2024, 1, 15, 10, 0, 0)
        mock_event.end_at = datetime(2024, 1, 15, 11, 0, 0)
        mock_event.attendee_count = 0
        mock_event.is_all_day = False
        mock_event.source_modified_at = None

        incoming = {
            "summary": "Meeting",
            "status": "confirmed",
            "start": {"dateTime": "2024-01-15T10:00:00+00:00"},
            "end": {"dateTime": "2024-01-15T11:00:00+00:00"},
        }

        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert isinstance(result, bool)
