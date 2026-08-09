"""Registry-backed binding validation and compilation tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from src.api.schemas.automation_trigger_models import AutomationTriggerCreateRequest
from src.service.automation.triggers.validator import AutomationTriggerValidator


class FakeOptions:
    """Represent the Fake Options test double used by the surrounding regression cases."""

    def __init__(self) -> None:
        self.stage_ids: set[UUID] = set()
        self.audience_ids: set[UUID] = set()
        self.validated_options: list[tuple[str, set[str]]] = []
        self.validated_sources: list[tuple[str, set[str], UUID, bool]] = []
        self.schedule_subjects: set[tuple[str, UUID]] = set()
        self.validated_schedule_subjects: list[tuple[str, set[UUID], UUID, bool]] = []
        self.validated_delivery_sources: list[tuple[str, set[str], UUID, bool]] = []

    async def validate_option_selection(self, source: str, values: set[str]) -> None:
        """Expose the validate option selection operation expected by the Fake Options test double."""
        self.validated_options.append((source, values))
        if source == "pipeline_stages" and {UUID(value) for value in values} - self.stage_ids:
            raise ValueError("Unknown CRM stage")

    async def validate_audience_user_ids(self, user_ids: set[UUID]) -> None:
        """Expose the validate audience user ids operation expected by the Fake Options test double."""
        if user_ids - self.audience_ids:
            raise ValueError("Unknown or inactive audience user")

    async def validate_source_selection(
        self,
        source: str,
        values: set[str],
        *,
        principal_id: UUID,
        organisation_scope: bool,
    ) -> None:
        self.validated_sources.append((source, values, principal_id, organisation_scope))
        if source == "email_mailboxes" and not organisation_scope:
            self.validated_delivery_sources.append(
                (source, values, principal_id, organisation_scope)
            )

    async def validate_schedule_subjects(
        self,
        subject_type: str,
        subject_ids: set[UUID],
        *,
        principal_id: UUID,
        organisation_scope: bool,
    ) -> None:
        """Expose the validate schedule subjects operation expected by the Fake Options test double."""
        self.validated_schedule_subjects.append(
            (subject_type, subject_ids, principal_id, organisation_scope)
        )
        if {(subject_type, item) for item in subject_ids} - self.schedule_subjects:
            raise ValueError("Unknown or unavailable scheduled subject")


def personal_body(
    trigger_key: str,
    **overrides: object,
) -> AutomationTriggerCreateRequest:
    """Build the personal body fixture used by the surrounding regression cases."""
    payload: dict[str, object] = {
        "name": "Review event",
        "trigger_key": trigger_key,
        "principal_mode": "personal",
        "scope": {"type": "owned_or_created_by_principal"},
        "audience": {"mode": "principal"},
        "task": {"goal": "Review the durable fact"},
    }
    payload.update(overrides)
    return AutomationTriggerCreateRequest.model_validate(payload)


def validator(options: FakeOptions | None = None, *, is_admin: bool = False):
    """Build the validator fixture used by the surrounding regression cases."""
    return AutomationTriggerValidator(
        options or FakeOptions(),
        organisation_id=uuid4(),
        user_id=uuid4(),
        is_admin=is_admin,
    )


@pytest.mark.asyncio
async def test_preset_compiles_fixed_matcher_and_ands_typed_user_filter() -> None:
    """Verify the contract that preset compiles fixed matcher and ands typed user filter."""
    result = await validator().validate(
        personal_body(
            "crm.deal.amount_crossed",
            config={"direction": "up", "threshold": "100000.00"},
            filter={"all": [{"path": "record.currency", "operator": "eq", "value": "USD"}]},
        )
    )

    assert result.trigger_config == {
        "direction": "up",
        "threshold": "100000.00",
        "selection": {"mode": "all", "ids": []},
    }
    assert result.watch_config == {"mode": "any", "paths": ["amount"]}
    assert result.filter_config == {
        "all": [
            {
                "all": [
                    {
                        "path": "changes.amount.before",
                        "operator": "lt",
                        "value": "100000.00",
                    },
                    {
                        "path": "changes.amount.after",
                        "operator": "gte",
                        "value": "100000.00",
                    },
                ]
            },
            {"all": [{"path": "record.currency", "operator": "eq", "value": "USD"}]},
        ]
    }


@pytest.mark.asyncio
async def test_only_generic_update_definition_accepts_user_watch() -> None:
    """Verify the contract that only generic update definition accepts user watch."""
    result = await validator().validate(
        personal_body(
            "crm.contact.updated",
            watch={"mode": "all", "paths": ["email", "phone"]},
        )
    )
    assert result.watch_config == {"mode": "all", "paths": ["email", "phone"]}

    with pytest.raises(ValueError, match="does not accept a user watch"):
        await validator().validate(
            personal_body(
                "crm.contact.email_changed",
                watch={"mode": "any", "paths": ["email"]},
            )
        )


@pytest.mark.asyncio
async def test_filter_paths_operators_and_values_are_registry_typed() -> None:
    """Verify the contract that filter paths operators and values are registry typed."""
    with pytest.raises(ValueError, match="decimal string"):
        await validator().validate(
            personal_body(
                "crm.deal.created",
                filter={"all": [{"path": "record.amount", "operator": "gte", "value": 100000}]},
            )
        )


@pytest.mark.asyncio
async def test_hidden_filter_fields_are_rejected_by_the_single_write_contract() -> None:
    with pytest.raises(ValueError, match="hidden filter field"):
        await validator().validate(
            personal_body(
                "crm.deal.created",
                filter={"path": "record.id", "operator": "eq", "value": str(uuid4())},
            )
        )

    with pytest.raises(ValueError, match="hidden filter field"):
        await validator().validate(
            personal_body(
                "crm.contact.updated",
                filter={
                    "path": "record.owner_id",
                    "operator": "eq",
                    "value": "crm-user-1",
                },
            )
        )


@pytest.mark.asyncio
async def test_organisation_mode_can_use_owner_as_a_business_filter() -> None:
    body = AutomationTriggerCreateRequest.model_validate(
        {
            "name": "Assigned contacts",
            "trigger_key": "crm.contact.updated",
            "principal_mode": "organisation",
            "scope": {"type": "organisation"},
            "audience": {"mode": "none"},
            "filter": {
                "path": "record.owner_id",
                "operator": "eq",
                "value": "crm-user-1",
            },
        }
    )

    result = await validator(is_admin=True).validate(body)

    assert result.filter_config == body.filter


@pytest.mark.asyncio
async def test_source_selection_is_validated_inside_the_resolved_authority() -> None:
    options = FakeOptions()
    item = validator(options)
    deal_id = uuid4()

    result = await item.validate(
        personal_body(
            "crm.deal.updated",
            config={"selection": {"mode": "selected", "ids": [str(deal_id)]}},
        )
    )

    assert result.trigger_config["selection"] == {
        "mode": "selected",
        "ids": [str(deal_id)],
    }
    assert options.validated_sources == [("deals", {str(deal_id)}, item.user_id, False)]


@pytest.mark.asyncio
async def test_email_mailbox_selection_compiles_to_hidden_actor_identity() -> None:
    options = FakeOptions()
    item = validator(options)
    mailbox_id = uuid4()

    result = await item.validate(
        personal_body(
            "email.message.received",
            config={"selection": {"mode": "selected", "ids": [str(mailbox_id)]}},
        )
    )

    assert result.filter_config == {
        "path": "actor.id",
        "operator": "in",
        "value": [str(mailbox_id)],
    }
    assert options.validated_sources == [
        ("email_mailboxes", {str(mailbox_id)}, item.user_id, False)
    ]


@pytest.mark.asyncio
async def test_source_selection_is_not_queried_before_organisation_admin_check() -> None:
    options = FakeOptions()
    body = AutomationTriggerCreateRequest.model_validate(
        {
            "name": "All account changes",
            "trigger_key": "crm.account.updated",
            "principal_mode": "organisation",
            "scope": {"type": "organisation"},
            "audience": {"mode": "none"},
            "config": {"selection": {"mode": "selected", "ids": [str(uuid4())]}},
        }
    )

    with pytest.raises(ValueError, match="organisation admin"):
        await validator(options).validate(body)

    assert options.validated_sources == []


@pytest.mark.asyncio
async def test_schedule_normalizes_and_projects_next_occurrence() -> None:
    """Verify the contract that schedule normalizes and projects next occurrence."""
    result = await validator().validate(
        personal_body(
            "time.schedule.due",
            config={
                "timezone": "Asia/Kolkata",
                "starts_at": "2026-07-14T09:00:00+05:30",
                "recurrence": "daily",
            },
        ),
        now=datetime(2026, 7, 13, 12, tzinfo=UTC),
    )

    assert result.trigger_config == {
        "timezone": "Asia/Kolkata",
        "starts_at": "2026-07-14T09:00:00+05:30",
        "recurrence": "daily",
    }
    assert result.next_trigger_at == datetime(2026, 7, 14, 3, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_schedule_subjects_are_tenant_and_principal_validated() -> None:
    """Verify the contract that schedule subjects are tenant and principal validated."""
    options = FakeOptions()
    subject_id = uuid4()
    options.schedule_subjects.add(("deal", subject_id))
    item = validator(options)

    result = await item.validate(
        personal_body(
            "time.schedule.due",
            config={
                "timezone": "UTC",
                "starts_at": "2026-08-06T09:00:00+00:00",
                "recurrence": "daily",
                "subject_type": "deal",
                "subject_ids": [str(subject_id)],
            },
        ),
        now=datetime(2026, 8, 5, 12, tzinfo=UTC),
    )

    assert result.trigger_config["subject_ids"] == [str(subject_id)]
    assert options.validated_schedule_subjects == [("deal", {subject_id}, item.user_id, False)]

    options.schedule_subjects.clear()
    with pytest.raises(ValueError, match="unavailable"):
        await item.validate(
            personal_body(
                "time.schedule.due",
                config=result.trigger_config,
            ),
            now=datetime(2026, 8, 5, 12, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_stage_config_is_tenant_validated() -> None:
    """Verify the contract that stage config is tenant validated."""
    options = FakeOptions()
    stage_id = uuid4()
    options.stage_ids.add(stage_id)

    result = await validator(options).validate(
        personal_body("crm.deal.entered_stage", config={"stage_id": str(stage_id)})
    )

    assert result.trigger_config == {
        "stage_id": str(stage_id),
        "selection": {"mode": "all", "ids": []},
    }
    assert options.validated_options == [("pipeline_stages", {str(stage_id)})]


@pytest.mark.asyncio
async def test_organisation_binding_requires_admin_safe_target_and_explicit_audience() -> None:
    """Verify the contract that organisation binding requires admin safe target and explicit audience."""
    audience_id = uuid4()
    options = FakeOptions()
    options.audience_ids.add(audience_id)
    body = AutomationTriggerCreateRequest.model_validate(
        {
            "name": "Shared review",
            "trigger_key": "crm.deal.created",
            "principal_mode": "organisation",
            "scope": {"type": "organisation"},
            "audience": {"mode": "explicit_users", "user_ids": [str(audience_id)]},
        }
    )

    with pytest.raises(ValueError, match="admin"):
        await validator(options).validate(body)
    with pytest.raises(ValueError, match="user-scoped tools"):
        await validator(options, is_admin=True).validate(
            body,
            target_allowed_tools=("search_deals",),
        )

    result = await validator(options, is_admin=True).validate(body)

    assert result.execution_principal_type == "organisation_service"
    assert result.audience_config == {"user_ids": [str(audience_id)]}


@pytest.mark.asyncio
async def test_automation_bindings_reject_retry_unsafe_mutating_tools() -> None:
    """Verify the contract that automation bindings reject retry unsafe mutating tools."""
    with pytest.raises(ValueError, match="retry-safe"):
        await validator().validate(
            personal_body("crm.deal.updated"),
            target_allowed_tools=("update_deal_fields",),
        )


def test_trigger_contract_rejects_output_delivery_as_a_separate_agent_decision() -> None:
    base = {
        "name": "Review event",
        "trigger_key": "crm.deal.created",
        "principal_mode": "personal",
        "scope": {"type": "owned_or_created_by_principal"},
        "audience": {"mode": "principal"},
        "delivery": {"mode": "email_notification"},
    }

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        AutomationTriggerCreateRequest.model_validate(base)


@pytest.mark.asyncio
async def test_organisation_bindings_reject_skills_until_service_policy_exists() -> None:
    """Verify the contract that organisation bindings reject skills until service policy exists."""
    body = AutomationTriggerCreateRequest.model_validate(
        {
            "name": "Shared review",
            "trigger_key": "crm.deal.created",
            "principal_mode": "organisation",
            "scope": {"type": "organisation"},
            "audience": {"mode": "none"},
        }
    )

    with pytest.raises(ValueError, match="skills"):
        await validator(is_admin=True).validate(
            body,
            target_allowed_skills=("deal-health-check",),
        )


@pytest.mark.asyncio
async def test_unavailable_registry_trigger_cannot_be_saved() -> None:
    """Verify the contract that unavailable registry trigger cannot be saved."""
    with pytest.raises(ValueError, match="producer is not available"):
        await validator().validate(personal_body("email.message.replied"))


@pytest.mark.asyncio
async def test_subject_participant_audience_requires_registry_backed_resolution() -> None:
    """Verify the contract that subject participant audience requires registry backed resolution."""
    supported = AutomationTriggerCreateRequest.model_validate(
        {
            "name": "Shared email review",
            "trigger_key": "email.message.received",
            "principal_mode": "organisation",
            "scope": {"type": "organisation"},
            "audience": {"mode": "subject_participants"},
        }
    )

    result = await validator(is_admin=True).validate(supported)
    assert result.audience_config == {}
    for trigger_key in ("email.message.received", "meeting.transcript.ready"):
        resolved = await validator(is_admin=True).validate(
            supported.model_copy(update={"trigger_key": trigger_key})
        )
        assert resolved.audience_config == {}
    for trigger_key in (
        "agent.run.completed",
        "crm.deal.created",
        "ai.deal_risk.detected",
    ):
        with pytest.raises(ValueError, match="audience"):
            await validator(is_admin=True).validate(
                supported.model_copy(update={"trigger_key": trigger_key})
            )


@pytest.mark.asyncio
async def test_resolved_ai_signal_validates_status_against_terminal_options() -> None:
    """Resolved AI events must not accept statuses used only by active signals."""

    options = FakeOptions()
    body = AutomationTriggerCreateRequest.model_validate(
        {
            "name": "Review resolved risks",
            "trigger_key": "ai.deal_risk.resolved",
            "principal_mode": "organisation",
            "scope": {"type": "organisation"},
            "audience": {"mode": "none"},
            "filter": {"all": [{"path": "record.status", "operator": "eq", "value": "RESOLVED"}]},
        }
    )

    await validator(options, is_admin=True).validate(body)

    assert ("risk_resolution_statuses", {"RESOLVED"}) in options.validated_options
    assert not any(source == "risk_statuses" for source, _values in options.validated_options)


@pytest.mark.asyncio
async def test_webhook_schema_allows_local_refs_but_rejects_external_resolution() -> None:
    """Verify the contract that webhook schema allows local refs but rejects external resolution."""
    local_schema = {
        "$defs": {"risk": {"type": "string"}},
        "type": "object",
        "properties": {"risk": {"$ref": "#/$defs/risk"}},
    }

    result = await validator().validate(
        personal_body(
            "integration.webhook.received",
            config={"payload_schema": local_schema},
        )
    )

    assert result.trigger_config["payload_schema"] == local_schema
    for reference in ("https://example.invalid/risk.json", "risk.json"):
        with pytest.raises(ValueError, match="external references"):
            await validator().validate(
                personal_body(
                    "integration.webhook.received",
                    config={
                        "payload_schema": {
                            "type": "object",
                            "properties": {"risk": {"$ref": reference}},
                        }
                    },
                )
            )
