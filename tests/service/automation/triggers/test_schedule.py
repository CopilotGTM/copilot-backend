"""DST-correct recurrence and transaction-local due schedule dispatch."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from src.domain.models.organisation_tables import AutomationTrigger
from src.service.automation.triggers.access import AutomationScheduleAccess
from src.service.automation.triggers.direct import ManualSubject
from src.service.automation.triggers.schedule import (
    SCHEDULE_SUBJECT_LIMIT,
    ScheduleConfig,
    ScheduleDispatchService,
    next_occurrence,
    normalize_schedule_config,
)


def config(
    *,
    starts_at: str = "2026-03-07T09:00:00-05:00",
    recurrence: str = "daily",
    timezone: str = "America/New_York",
    **extra: object,
) -> dict[str, object]:
    """Build the config fixture used by the surrounding regression cases."""
    return {
        "timezone": timezone,
        "starts_at": starts_at,
        "recurrence": recurrence,
        **extra,
    }


def test_recurrence_families_advance_from_the_exact_occurrence() -> None:
    """Verify the contract that recurrence families advance from the exact occurrence."""
    previous = datetime(2026, 1, 30, 9, tzinfo=UTC)
    expected = {
        "none": None,
        "daily": datetime(2026, 1, 31, 9, tzinfo=UTC),
        "weekdays": datetime(2026, 2, 2, 9, tzinfo=UTC),
        "weekly": datetime(2026, 2, 6, 9, tzinfo=UTC),
        "biweekly": datetime(2026, 2, 13, 9, tzinfo=UTC),
        "monthly": datetime(2026, 2, 28, 9, tzinfo=UTC),
        "custom": datetime(2026, 2, 2, 9, tzinfo=UTC),
    }

    for recurrence, next_at in expected.items():
        value = config(
            starts_at="2026-01-30T09:00:00+00:00",
            recurrence=recurrence,
            timezone="UTC",
            **({"interval_days": 3} if recurrence == "custom" else {}),
        )
        assert next_occurrence(value, previous) == next_at

    _normalized, first_weekday = normalize_schedule_config(
        config(
            starts_at="2026-01-31T09:00:00+00:00",
            recurrence="weekdays",
            timezone="UTC",
        ),
        now=datetime(2026, 1, 30, 8, tzinfo=UTC),
    )
    assert first_weekday == datetime(2026, 2, 2, 9, tzinfo=UTC)


def test_daily_schedule_preserves_wall_clock_across_dst_boundaries() -> None:
    """Verify the contract that daily schedule preserves wall clock across dst boundaries."""
    morning = config()
    assert next_occurrence(morning, datetime(2026, 3, 7, 14, tzinfo=UTC)) == datetime(
        2026, 3, 8, 13, tzinfo=UTC
    )

    spring_gap = config(starts_at="2026-03-07T02:30:00-05:00")
    assert next_occurrence(
        spring_gap,
        datetime(2026, 3, 7, 7, 30, tzinfo=UTC),
    ) == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)

    fall_back = config(starts_at="2026-10-31T01:30:00-04:00")
    assert next_occurrence(
        fall_back,
        datetime(2026, 10, 31, 5, 30, tzinfo=UTC),
    ) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)


def test_schedule_validation_rejects_unknown_zones_and_unbounded_subjects() -> None:
    """Verify the contract that schedule validation rejects unknown zones and unbounded subjects."""
    with pytest.raises(ValueError, match="IANA"):
        normalize_schedule_config(
            config(timezone="Mars/Olympus_Mons"),
            now=datetime(2026, 3, 1, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_due_schedule_claim_uses_tenant_filter_and_skip_locked() -> None:
    """Verify the contract that due schedule claim uses tenant filter and skip locked."""
    organisation_id = uuid4()

    class Scalars:
        """Represent the Scalars test double used by the surrounding regression cases."""

        def all(self):
            """Build the all fixture used by the surrounding regression cases."""
            return []

    class Result:
        """Represent the Result test double used by the surrounding regression cases."""

        def scalars(self):
            """Expose the scalars operation expected by the Result test double."""
            return Scalars()

    class SQLSession:
        """Represent the SQLSession test double used by the surrounding regression cases."""

        statement = None

        async def execute(self, statement):  # noqa: ANN001
            """Expose the execute operation expected by the SQLSession test double."""
            self.statement = statement
            return Result()

    session = SQLSession()
    await AutomationScheduleAccess(  # type: ignore[arg-type]
        session,
        organisation_id,
    ).claim_due_schedules(
        now=datetime(2026, 8, 5, 10, tzinfo=UTC),
        limit=25,
    )

    compiled = session.statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "automation_triggers.organisation_id" in sql
    assert "automation_triggers.next_trigger_at <=" in sql
    assert "LIMIT" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert organisation_id in compiled.params.values()

    with pytest.raises(ValidationError):
        ScheduleConfig.model_validate(
            config(
                subject_type="deal",
                subject_ids=[str(uuid4()) for _ in range(SCHEDULE_SUBJECT_LIMIT + 1)],
            )
        )


def trigger(  # noqa: ANN001
    organisation_id,
    principal_id,
    *,
    occurrence,
    subjects=(),
    recurrence="daily",
):
    """Build the trigger fixture used by the surrounding regression cases."""
    return AutomationTrigger(
        organisation_id=organisation_id,
        created_by_user_id=principal_id,
        name="Scheduled review",
        target_id=uuid4(),
        target_revision=1,
        target_snapshot={},
        trigger_key="time.schedule.due",
        family="schedule",
        match_event_keys=["time.schedule.due"],
        trigger_config={
            "timezone": "UTC",
            "starts_at": "2026-08-05T10:00:00+00:00",
            "recurrence": recurrence,
            **(
                {
                    "subject_type": subjects[0].subject_type,
                    "subject_ids": [str(item.subject_id) for item in subjects],
                }
                if subjects
                else {}
            ),
        },
        execution_principal_type="user",
        execution_principal_id=principal_id,
        scope_type="owned_or_created_by_principal",
        audience_mode="principal",
        config_hash="b" * 64,
        status="active",
        next_trigger_at=occurrence,
    )


class Access:
    """Represent the Access test double used by the surrounding regression cases."""

    def __init__(self, rows):  # noqa: ANN001
        self.rows = rows
        self.calls = []

    async def claim_due_schedules(self, *, now, limit):  # noqa: ANN001
        """Expose the claim due schedules operation expected by the Access test double."""
        self.calls.append((now, limit))
        return self.rows


class SubjectAccess:
    """Represent the Subject Access test double used by the surrounding regression cases."""

    def __init__(self, records):  # noqa: ANN001
        self.records = records

    async def load_subject_records(
        self,
        subjects,
        *,
        principal_id,
        organisation_scope,  # noqa: ANN001
    ):
        """Expose the load subject records operation expected by the Subject Access test double."""
        assert organisation_scope is False
        assert principal_id
        return {
            item.identity: self.records[item.identity]
            for item in subjects
            if item.identity in self.records
        }


class Enqueuer:
    """Represent the Enqueuer test double used by the surrounding regression cases."""

    def __init__(self):
        self.envelopes = []

    async def enqueue(self, envelope):  # noqa: ANN001
        """Build the enqueue fixture used by the surrounding regression cases."""
        self.envelopes.append(envelope)
        return object()


class Session:
    """Represent the Session test double used by the surrounding regression cases."""

    def __init__(self):
        self.flushed = False

    async def flush(self):
        """Expose the flush operation expected by the Session test double."""
        self.flushed = True


@pytest.mark.asyncio
async def test_due_schedule_fans_out_and_advances_in_the_same_transaction() -> None:
    """Verify the contract that due schedule fans out and advances in the same transaction."""
    organisation_id, principal_id = uuid4(), uuid4()
    occurrence = datetime(2026, 8, 5, 10, tzinfo=UTC)
    subjects = (ManualSubject("deal", uuid4()), ManualSubject("deal", uuid4()))
    binding = trigger(organisation_id, principal_id, occurrence=occurrence, subjects=subjects)
    records = {
        item.identity: {"id": str(item.subject_id), "owner_id": str(principal_id)}
        for item in subjects
    }
    access, enqueuer, session = Access([binding]), Enqueuer(), Session()

    result = await ScheduleDispatchService(
        session,  # type: ignore[arg-type]
        organisation_id,
        access=access,
        subject_access=SubjectAccess(records),
        event_enqueuer=enqueuer,
    ).dispatch_due(now=occurrence, limit=25)

    assert result.claimed_count == 1
    assert result.dispatched_count == 2
    assert len({item.event_id for item in enqueuer.envelopes}) == 2
    assert {item.subject.id for item in enqueuer.envelopes} == {
        str(subject.subject_id) for subject in subjects
    }
    assert {item.attributes["scheduled_for"] for item in enqueuer.envelopes} == {
        occurrence.isoformat()
    }
    assert len({item.correlation_id for item in enqueuer.envelopes}) == 1
    assert binding.next_trigger_at == datetime(2026, 8, 6, 10, tzinfo=UTC)
    assert session.flushed is True


@pytest.mark.asyncio
async def test_one_time_schedule_remains_active_until_its_queued_event_activates() -> None:
    """Keep the direct binding eligible after its only occurrence is enqueued."""
    organisation_id, principal_id = uuid4(), uuid4()
    occurrence = datetime(2026, 8, 5, 10, tzinfo=UTC)
    binding = trigger(
        organisation_id,
        principal_id,
        occurrence=occurrence,
        recurrence="none",
    )
    enqueuer = Enqueuer()

    result = await ScheduleDispatchService(
        Session(),  # type: ignore[arg-type]
        organisation_id,
        access=Access([binding]),
        event_enqueuer=enqueuer,
    ).dispatch_due(now=occurrence)

    assert result.dispatched_count == 1
    assert len(enqueuer.envelopes) == 1
    assert binding.next_trigger_at is None
    assert binding.status == "active"


@pytest.mark.asyncio
async def test_due_schedule_pauses_when_a_saved_subject_is_no_longer_available() -> None:
    """Verify the contract that due schedule pauses when a saved subject is no longer available."""
    organisation_id, principal_id = uuid4(), uuid4()
    occurrence = datetime(2026, 8, 5, 10, tzinfo=UTC)
    subject = ManualSubject("deal", uuid4())
    binding = trigger(
        organisation_id,
        principal_id,
        occurrence=occurrence,
        subjects=(subject,),
    )
    enqueuer = Enqueuer()

    result = await ScheduleDispatchService(
        Session(),  # type: ignore[arg-type]
        organisation_id,
        access=Access([binding]),
        subject_access=SubjectAccess({}),
        event_enqueuer=enqueuer,
    ).dispatch_due(now=occurrence)

    assert result.paused_count == 1
    assert enqueuer.envelopes == []
    assert binding.status == "paused"
    assert binding.last_error_code == "schedule_subject_unavailable"
