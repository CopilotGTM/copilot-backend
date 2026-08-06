"""Privacy and replay contracts for durable meeting events."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.calendar import GenerationStatus
from src.domain.models.organisation_tables.sync.meeting_intelligence import (
    Meeting,
    MeetingSource,
    MeetingStatus,
)
from src.service.automation.producers.meeting import (
    MeetingEventAdapter,
    MeetingEventContext,
)


def meeting(organisation_id):
    """Build the meeting fixture used by the surrounding regression cases."""
    return Meeting(
        organisation_id=organisation_id,
        calendar_event_id=uuid4(),
        source=MeetingSource.NATIVE_BOT,
        status=MeetingStatus.READY,
        full_transcript="private transcript text",
        transcript_fetched_at=datetime(2026, 8, 5, 9, tzinfo=UTC),
        transcript_language="en-US",
        source_data={"provider_payload": "private provider data"},
        meeting_summary_status=GenerationStatus.COMPLETED,
        meeting_summary_data={"summary": "private summary text"},
        meeting_summary_generated_at=datetime(2026, 8, 5, 10, tzinfo=UTC),
    )


def test_transcript_ready_is_reference_only_and_replay_stable() -> None:
    """Verify the contract that transcript ready is reference only and replay stable."""
    organisation_id = uuid4()
    record = meeting(organisation_id)
    owner_id = uuid4()
    attendee_id = uuid4()
    context = MeetingEventContext(
        owner_id=owner_id,
        attendee_ids=(owner_id, attendee_id),
        team_ids=("sales",),
        deal_id=uuid4(),
    )
    adapter = MeetingEventAdapter(organisation_id)

    first = adapter.transcript_ready(record, context=context)
    replay = adapter.transcript_ready(record, context=context)

    assert first.event_key == "meeting.transcript.ready"
    assert first.event_id == replay.event_id
    assert first.idempotency_key == replay.idempotency_key
    assert first.record == {
        "id": str(record.meeting_id),
        "provider": "native_bot",
        "language": "en-US",
        "owner_id": str(owner_id),
        "team_ids": ["sales"],
        "attendee_ids": [str(owner_id), str(attendee_id)],
        "deal_id": str(context.deal_id),
    }
    assert first.references == {"meeting_id": str(record.meeting_id)}
    assert first.audience_user_ids == (owner_id, attendee_id)
    serialized = first.model_dump_json()
    for forbidden in (
        "private transcript text",
        "private provider data",
        "private summary text",
    ):
        assert forbidden not in serialized


def test_summary_ready_uses_the_durable_summary_fact_identity() -> None:
    """Verify the contract that summary ready uses the durable summary fact identity."""
    organisation_id = uuid4()
    record = meeting(organisation_id)
    summary_fact_id = uuid4()
    context = MeetingEventContext(deal_id=uuid4())

    envelope = MeetingEventAdapter(organisation_id).summary_ready(
        record,
        summary_fact_id=summary_fact_id,
        context=context,
    )

    assert envelope.event_key == "meeting.summary.ready"
    assert envelope.source.source_event_id == str(summary_fact_id)
    assert envelope.idempotency_key == f"meeting.summary.ready:{summary_fact_id}"
    assert envelope.subject.id == str(record.meeting_id)
    assert "private summary text" not in envelope.model_dump_json()


def test_meeting_adapter_rejects_cross_tenant_and_non_durable_states() -> None:
    """Verify the contract that meeting adapter rejects cross tenant and non durable states."""
    organisation_id = uuid4()
    record = meeting(organisation_id)
    adapter = MeetingEventAdapter(uuid4())

    with pytest.raises(ValueError, match="organisation"):
        adapter.transcript_ready(record, context=MeetingEventContext())

    record.organisation_id = organisation_id
    record.status = MeetingStatus.PROCESSING
    with pytest.raises(ValueError, match="ready"):
        MeetingEventAdapter(organisation_id).transcript_ready(
            record,
            context=MeetingEventContext(),
        )
