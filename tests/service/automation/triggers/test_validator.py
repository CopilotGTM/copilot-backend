"""Validation tests for the two trigger contracts supported in V1."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from src.api.schemas.automation_trigger_models import AutomationTriggerCreateRequest
from src.domain.models.organisation_tables import AutomationTriggerType
from src.service.automation.triggers.validator import AutomationTriggerValidator


class FakeOptions:
    def __init__(self, pipeline_id: UUID, stage_ids: set[UUID]) -> None:
        self.pipeline_id = pipeline_id
        self.stage_ids = stage_ids

    async def validate_crm_selection(self, pipeline_ids: set[UUID], stage_ids: set[UUID]) -> None:
        if pipeline_ids - {self.pipeline_id}:
            raise ValueError("Unknown CRM pipeline")
        if stage_ids - self.stage_ids:
            raise ValueError("Unknown CRM stage")


def schedule_body(**overrides: object) -> AutomationTriggerCreateRequest:
    payload: dict[str, object] = {
        "name": "Morning review",
        "trigger_type": AutomationTriggerType.SCHEDULE,
        "trigger_config": {
            "timezone": "Asia/Kolkata",
            "starts_at": "2026-07-14T09:00:00+05:30",
            "recurrence": "daily",
        },
        "scope_config": {"entity": "deal", "statuses": ["open"]},
        "task_config": {"goal": "Review deals"},
    }
    payload.update(overrides)
    return AutomationTriggerCreateRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_schedule_normalizes_and_projects_next_occurrence() -> None:
    result = await AutomationTriggerValidator(FakeOptions(uuid4(), set())).validate(
        schedule_body(), now=datetime(2026, 7, 13, 12, tzinfo=UTC)
    )

    assert result.trigger_config == {
        "timezone": "Asia/Kolkata",
        "starts_at": "2026-07-14T09:00:00+05:30",
        "recurrence": "daily",
    }
    assert result.next_trigger_at == datetime(2026, 7, 14, 3, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_schedule_keeps_month_end_anchor_and_weekdays_skip_weekends() -> None:
    validator = AutomationTriggerValidator(FakeOptions(uuid4(), set()))
    monthly = await validator.validate(
        schedule_body(
            trigger_config={
                "timezone": "UTC",
                "starts_at": "2026-01-31T09:00:00+00:00",
                "recurrence": "monthly",
            }
        ),
        now=datetime(2026, 3, 1, tzinfo=UTC),
    )
    weekday = await validator.validate(
        schedule_body(
            trigger_config={
                "timezone": "UTC",
                "starts_at": "2026-07-18T09:00:00+00:00",
                "recurrence": "weekdays",
            }
        ),
        now=datetime(2026, 7, 17, tzinfo=UTC),
    )

    assert monthly.next_trigger_at == datetime(2026, 3, 31, 9, tzinfo=UTC)
    assert weekday.next_trigger_at == datetime(2026, 7, 20, 9, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trigger_config", "message"),
    [
        (
            {
                "timezone": "Mars/Base",
                "starts_at": "2026-07-14T09:00:00+05:30",
                "recurrence": "none",
            },
            "timezone",
        ),
        ({"timezone": "UTC", "starts_at": "2026-07-14T09:00:00", "recurrence": "none"}, "timezone"),
        (
            {"timezone": "UTC", "starts_at": "2026-07-12T09:00:00+00:00", "recurrence": "none"},
            "future",
        ),
    ],
)
async def test_schedule_rejects_unrunnable_configuration(
    trigger_config: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        await AutomationTriggerValidator(FakeOptions(uuid4(), set())).validate(
            schedule_body(trigger_config=trigger_config),
            now=datetime(2026, 7, 13, 12, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_crm_specific_stage_requires_one_owned_pipeline_and_owned_stages() -> None:
    pipeline_id, from_stage, to_stage = uuid4(), uuid4(), uuid4()
    body = AutomationTriggerCreateRequest.model_validate(
        {
            "name": "Stage review",
            "trigger_type": "event",
            "source_domain": "crm",
            "event_type": "crm.deal.stage_changed",
            "filter_config": {
                "schema_key": "crm.deal.stage_changed.v1",
                "pipeline_ids": [str(pipeline_id)],
                "stage_change_mode": "specific_stage",
                "from_stage_ids": [str(from_stage)],
                "to_stage_ids": [str(to_stage)],
                "outcome": "any",
            },
        }
    )

    result = await AutomationTriggerValidator(
        FakeOptions(pipeline_id, {from_stage, to_stage})
    ).validate(body, now=datetime(2026, 7, 13, tzinfo=UTC))

    assert result.next_trigger_at is None
    assert result.filter_config["pipeline_ids"] == [str(pipeline_id)]


@pytest.mark.asyncio
async def test_crm_rejects_wrong_identity_and_invalid_mode_combinations() -> None:
    validator = AutomationTriggerValidator(FakeOptions(uuid4(), set()))
    base = {
        "schema_key": "crm.deal.stage_changed.v1",
        "pipeline_ids": [],
        "stage_change_mode": "outcome",
        "from_stage_ids": [],
        "to_stage_ids": [],
        "outcome": "any",
    }
    with pytest.raises(ValueError, match="source_domain"):
        await validator.validate(
            AutomationTriggerCreateRequest.model_validate(
                {
                    "name": "Wrong source",
                    "trigger_type": "event",
                    "source_domain": "email",
                    "event_type": "crm.deal.stage_changed",
                    "filter_config": base,
                }
            ),
            now=datetime.now(UTC),
        )
    with pytest.raises(ValueError, match="outcome"):
        await validator.validate(
            AutomationTriggerCreateRequest.model_validate(
                {
                    "name": "Missing outcome",
                    "trigger_type": "event",
                    "source_domain": "crm",
                    "event_type": "crm.deal.stage_changed",
                    "filter_config": base,
                }
            ),
            now=datetime.now(UTC),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type",
    [
        "crm.contact.created",
        "crm.contact.deleted",
        "crm.account.created",
        "crm.account.deleted",
    ],
)
async def test_crm_lifecycle_events_accept_only_their_versioned_schema(event_type: str) -> None:
    validator = AutomationTriggerValidator(FakeOptions(uuid4(), set()))
    body = AutomationTriggerCreateRequest.model_validate(
        {
            "name": "Lifecycle agent",
            "trigger_type": "event",
            "source_domain": "crm",
            "event_type": event_type,
            "filter_config": {"schema_key": f"{event_type}.v1"},
        }
    )

    result = await validator.validate(body)

    assert result.filter_config == {"schema_key": f"{event_type}.v1"}

    body.filter_config = {"schema_key": "crm.deal.created.v1"}
    with pytest.raises(ValueError, match="schema_key"):
        await validator.validate(body)


@pytest.mark.asyncio
async def test_crm_deal_created_validates_pipeline_filter() -> None:
    pipeline_id = uuid4()
    body = AutomationTriggerCreateRequest.model_validate(
        {
            "name": "New deal agent",
            "trigger_type": "event",
            "source_domain": "crm",
            "event_type": "crm.deal.created",
            "filter_config": {
                "schema_key": "crm.deal.created.v1",
                "pipeline_ids": [str(pipeline_id)],
            },
        }
    )

    result = await AutomationTriggerValidator(FakeOptions(pipeline_id, set())).validate(body)

    assert result.filter_config == {
        "schema_key": "crm.deal.created.v1",
        "pipeline_ids": [str(pipeline_id)],
    }
