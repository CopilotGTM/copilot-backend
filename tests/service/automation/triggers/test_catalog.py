"""Regression tests for test catalog; these cases lock the backend contract used by the changed feature."""

from __future__ import annotations

from uuid import uuid4

import pytest
from src.contracts.automation_trigger_models import (
    TriggerCatalogPipelineOption,
    TriggerCatalogStageOption,
)
from src.service.automation.triggers.catalog import build_trigger_catalog
from src.service.automation.triggers.registry import AUTOMATION_REGISTRY


def _entries(catalog):
    return {
        trigger.trigger_key: trigger
        for category in catalog.categories
        for resource in category.resources
        for trigger in resource.triggers
    }


def test_catalog_serializes_registry_hierarchy_and_organisation_options() -> None:
    """Verify the contract that catalog serializes registry hierarchy and organisation options."""
    pipeline_id = uuid4()
    stage_id = uuid4()
    catalog = build_trigger_catalog(
        [
            TriggerCatalogPipelineOption(
                pipeline_id=pipeline_id,
                label="Sales Pipeline",
                is_active=True,
                stages=[
                    TriggerCatalogStageOption(
                        stage_id=stage_id,
                        label="Negotiation",
                        stage_state="open",
                        is_active=True,
                    )
                ],
            )
        ]
    )

    assert "schema_version" not in catalog.model_dump()
    assert catalog.limits.webhook_max_body_bytes == 60 * 1024
    assert catalog.limits.max_direct_subject_ids == 100
    assert catalog.limits.max_filter_group_levels == 2
    assert catalog.limits.max_filter_conditions == 20
    assert [category.category_key for category in catalog.categories][:4] == [
        "system",
        "time",
        "crm",
        "communication",
    ]
    crm = next(category for category in catalog.categories if category.category_key == "crm")
    assert [resource.resource_key for resource in crm.resources] == [
        "deal",
        "contact",
        "account",
    ]
    assert catalog.option_sets.pipelines[0].pipeline_id == pipeline_id
    assert catalog.option_sets.pipelines[0].stages[0].stage_id == stage_id
    assert [item.value for item in catalog.option_sets.enums["deal_statuses"]] == [
        "open",
        "won",
        "lost",
        "closed",
    ]
    assert "delivery_channels" not in type(catalog).model_fields


def test_catalog_exposes_business_fields_watch_labels_and_built_in_rules() -> None:
    """Expose business controls without returning compiled matcher implementation details."""
    entries = _entries(build_trigger_catalog([]))
    amount_crossed = entries["crm.deal.amount_crossed"]
    created_or_updated = entries["crm.deal.created_or_updated"]
    amount_path = next(field for field in amount_crossed.fields if field.path == "record.amount")

    assert amount_crossed.family == "event"
    assert amount_crossed.match_event_keys == ["crm.deal.updated"]
    assert amount_crossed.rule_presentation.summary == ("Amount crosses the configured threshold")
    assert [field.key for field in amount_crossed.config_fields] == ["direction", "threshold"]
    assert amount_path.value_type == "decimal"
    assert amount_path.input_type == "number"
    assert amount_path.group == "current"
    assert amount_path.visibility == "hidden"
    assert amount_path.operators == [
        "eq",
        "neq",
        "gt",
        "gte",
        "lt",
        "lte",
        "between",
        "is_empty",
        "not_empty",
    ]
    assert created_or_updated.match_event_keys == [
        "crm.deal.created",
        "crm.deal.updated",
    ]
    assert created_or_updated.allow_user_watch is True
    assert created_or_updated.rule_presentation.summary == (
        "Creates always run; updates run only when one of the selected fields changes"
    )
    watch_fields = {item.path: item.label for item in created_or_updated.watch_fields}
    assert watch_fields["amount"] == "Amount"
    assert "id" not in watch_fields
    assert "created_by_user_id" not in watch_fields
    serialized = created_or_updated.model_dump()
    assert "watch_paths" not in serialized
    assert "fixed_watch" not in serialized
    assert "fixed_filter" not in serialized


@pytest.mark.parametrize(
    ("trigger_key", "has_source_selector", "rule_summary"),
    [
        ("crm.deal.created", False, None),
        ("crm.deal.updated", True, None),
        ("crm.deal.deleted", True, None),
        (
            "crm.deal.created_or_updated",
            False,
            "Creates always run; updates run only when one of the selected fields changes",
        ),
        ("crm.deal.stage_changed", True, "Stage changed"),
        ("crm.deal.entered_stage", True, "New stage is the selected stage"),
        ("crm.deal.exited_stage", True, "Previous stage is the selected stage"),
        ("crm.deal.won", True, "New deal status is Won"),
        ("crm.deal.lost", True, "New deal status is Lost"),
        ("crm.deal.reopened", True, "New deal status is Open"),
        ("crm.deal.amount_changed", True, "Amount changes"),
        ("crm.deal.expected_close_date_changed", True, "Close date changes"),
        ("crm.deal.owner_id_changed", True, "Owner changes"),
        ("crm.deal.account_id_changed", True, "Account changes"),
        ("crm.deal.primary_contact_id_changed", True, "Primary contact changes"),
        (
            "crm.deal.amount_crossed",
            True,
            "Amount crosses the configured threshold",
        ),
    ],
)
def test_deal_catalog_matches_the_exact_business_trigger_matrix(
    trigger_key: str,
    has_source_selector: bool,
    rule_summary: str | None,
) -> None:
    deal_entries = {
        key: value
        for key, value in _entries(build_trigger_catalog([])).items()
        if key.startswith("crm.deal.")
    }

    assert len(deal_entries) == 16
    definition = deal_entries[trigger_key]
    assert bool(definition.source_selector) is has_source_selector
    assert definition.scope_presentation.model_dump() == {
        "personal": "Deals you own or created",
        "organisation": "All organisation deals",
    }
    assert (
        definition.rule_presentation.summary if definition.rule_presentation else None
    ) == rule_summary


@pytest.mark.parametrize(
    ("trigger_key", "standard_paths"),
    [
        (
            "email.message.received",
            {
                "record.sender_email",
                "record.sender_domain",
                "record.recipient_emails",
                "record.has_attachments",
                "record.deal_id",
            },
        ),
        (
            "email.message.sent",
            {
                "record.recipient_emails",
                "record.has_attachments",
                "record.deal_id",
            },
        ),
    ],
)
def test_email_catalog_exposes_only_business_conditions(
    trigger_key: str,
    standard_paths: set[str],
) -> None:
    definition = _entries(build_trigger_catalog([]))[trigger_key]

    assert {
        field.path for field in definition.fields if field.visibility == "standard"
    } == standard_paths
    assert definition.scope_presentation.model_dump() == {
        "personal": "New messages in active mailboxes connected by you",
        "organisation": "New messages in all active organisation mailboxes",
    }
    assert definition.source_selector.model_dump() == {
        "label": "Mailboxes",
        "all_label_personal": "All my connected mailboxes",
        "all_label_organisation": "All organisation mailboxes",
        "selected_label": "Specific mailboxes",
        "option_source": "email_mailboxes",
        "filter_path": "actor.id",
        "max_selected": 100,
    }
    assert definition.timing_note == (
        "Starts with new mail after this trigger is active; imported history is excluded."
    )
    serialized = str(definition.model_dump())
    assert "mailbox_integration_id" not in serialized
    assert "content_id" not in serialized


@pytest.mark.parametrize(
    ("trigger_key", "has_source_selector", "rule_summary"),
    [
        ("crm.contact.created", False, None),
        ("crm.contact.updated", True, None),
        ("crm.contact.deleted", True, None),
        (
            "crm.contact.created_or_updated",
            False,
            "Creates always run; updates run only when one of the selected fields changes",
        ),
        ("crm.contact.email_changed", True, "Email changes"),
        ("crm.contact.phone_changed", True, "Phone changes"),
        ("crm.contact.owner_id_changed", True, "Owner changes"),
        ("crm.contact.account_id_changed", True, "Account changes"),
        ("crm.contact.lifecycle_stage_changed", True, "Lifecycle changes"),
    ],
)
def test_contact_catalog_matches_the_exact_business_trigger_matrix(
    trigger_key: str,
    has_source_selector: bool,
    rule_summary: str | None,
) -> None:
    entries = _entries(build_trigger_catalog([]))
    contact_entries = {
        key: value for key, value in entries.items() if key.startswith("crm.contact.")
    }

    assert len(contact_entries) == 9
    definition = contact_entries[trigger_key]
    assert bool(definition.source_selector) is has_source_selector
    assert definition.scope_presentation.model_dump() == {
        "personal": "Contacts currently assigned to you",
        "organisation": "All organisation contacts",
    }
    assert "created by" not in definition.scope_presentation.personal.lower()
    assert (
        definition.rule_presentation.summary if definition.rule_presentation else None
    ) == rule_summary


@pytest.mark.parametrize(
    ("trigger_key", "has_source_selector", "rule_summary"),
    [
        ("crm.account.created", False, None),
        ("crm.account.updated", True, None),
        ("crm.account.deleted", True, None),
        (
            "crm.account.created_or_updated",
            False,
            "Creates always run; updates run only when one of the selected fields changes",
        ),
        ("crm.account.domain_changed", True, "Domain changes"),
        ("crm.account.owner_id_changed", True, "Owner changes"),
        ("crm.account.lifecycle_stage_changed", True, "Lifecycle changes"),
    ],
)
def test_account_catalog_matches_the_exact_released_business_trigger_matrix(
    trigger_key: str,
    has_source_selector: bool,
    rule_summary: str | None,
) -> None:
    entries = _entries(build_trigger_catalog([]))
    account_entries = {
        key: value for key, value in entries.items() if key.startswith("crm.account.")
    }

    assert len(account_entries) == 7
    assert "crm.account.tier_changed" not in account_entries
    assert AUTOMATION_REGISTRY.trigger("crm.account.tier_changed").producer_available is False
    definition = account_entries[trigger_key]
    assert bool(definition.source_selector) is has_source_selector
    assert definition.scope_presentation.model_dump() == {
        "personal": "Accounts currently assigned to you",
        "organisation": "All organisation accounts",
    }
    assert "created by" not in definition.scope_presentation.personal.lower()
    assert (
        definition.rule_presentation.summary if definition.rule_presentation else None
    ) == rule_summary


def test_catalog_hides_unavailable_producers_and_removed_correlated_fields() -> None:
    """Keep removed correlated trigger fields out of the single catalog contract."""
    catalog = build_trigger_catalog([])
    entries = _entries(catalog)

    assert "conversation.message.received" not in entries
    assert "agent.approval.requested" not in entries
    assert "meeting.scheduled" not in entries
    assert "meeting.transcript.ready" in entries
    assert "crm.lead.created" not in entries
    assert "crm.task.created" not in entries
    assert "crm.note.created" not in entries
    assert "ai.deal_risk.detected" in entries
    assert "ai.deal_blocker.resolved" in entries
    assert "ai.buyer_signal.detected" not in entries
    assert "agent.run.completed" in entries
    assert "agent.run.failed" in entries
    assert "agent.run.cancelled" not in entries

    serialized = entries["crm.deal.stage_changed"].model_dump()
    assert "trigger_type" not in serialized
    assert "source_domain" not in serialized
    assert "event_type" not in serialized
    assert serialized["rule_presentation"]["summary"] == "Stage changed"


def test_catalog_keeps_ai_risk_and_blocker_resources_distinct() -> None:
    """Keep independently labelled AI sources from collapsing into one resource."""
    ai = next(
        category
        for category in build_trigger_catalog([]).categories
        if category.category_key == "ai"
    )

    resources = {resource.resource_key: resource for resource in ai.resources}

    assert resources["deal_risk"].label == "Deal risk"
    assert resources["deal_blocker"].label == "Deal blocker"
    assert "ai.deal_risk.detected" in {
        trigger.trigger_key for trigger in resources["deal_risk"].triggers
    }
    assert "ai.deal_blocker.resolved" in {
        trigger.trigger_key for trigger in resources["deal_blocker"].triggers
    }


def test_every_catalog_path_is_unique_and_declares_matcher_operators() -> None:
    """Verify the contract that every catalog path is unique and declares matcher operators."""
    for definition in _entries(build_trigger_catalog([])).values():
        paths = [field.path for field in definition.fields]
        assert len(paths) == len(set(paths))
        assert all(field.operators for field in definition.fields)


def test_catalog_exposes_truthful_scope_timing_and_principal_visibility() -> None:
    entries = _entries(build_trigger_catalog([]))
    deal_created = entries["crm.deal.created"]
    deal_updated = entries["crm.deal.updated"]
    entered_stage = entries["crm.deal.entered_stage"]
    contact_updated = entries["crm.contact.updated"]

    assert deal_created.scope_presentation.model_dump() == {
        "personal": "Deals you own or created",
        "organisation": "All organisation deals",
    }
    assert deal_created.timing_note == "Starts with deals created after this trigger is active."
    assert entered_stage.rule_presentation.summary == "New stage is the selected stage"

    deal_fields = {item.path: item for item in deal_updated.fields}
    assert deal_fields["record.id"].visibility == "hidden"
    assert deal_fields["record.amount"].group == "current"
    assert deal_fields["record.stage_id"].input_type == "single_select"
    assert deal_fields["record.owner_id"].input_type == "entity_single_select"
    assert deal_fields["changes.amount.before"].group == "previous"
    assert deal_fields["changes.amount.after"].group == "new"
    assert deal_fields["source.source_event_id"].visibility == "hidden"
    assert deal_fields["record.amount"].event_keys == ["crm.deal.updated"]

    entered_fields = {item.path: item for item in entered_stage.fields}
    assert entered_fields["record.stage_id"].visibility == "hidden"
    assert entered_fields["changes.stage_id.after"].visibility == "hidden"
    assert entered_fields["changes.stage_id.before"].visibility == "standard"

    contact_fields = {item.path: item for item in contact_updated.fields}
    assert contact_fields["record.owner_id"].principal_modes == ["organisation"]
    assert contact_fields["changes.owner_id.after"].principal_modes == ["organisation"]
    assert contact_fields["changes.owner_id.before"].principal_modes == [
        "personal",
        "organisation",
    ]


def test_catalog_returns_only_the_47_producer_proven_definitions() -> None:
    entries = _entries(build_trigger_catalog([]))

    assert len(entries) == 47
    assert "crm.account.tier_changed" not in entries
    assert AUTOMATION_REGISTRY.trigger("crm.account.tier_changed").producer_available is False


def test_crm_catalog_truthfully_names_the_native_source_boundary() -> None:
    entries = _entries(build_trigger_catalog([]))

    for key, definition in entries.items():
        if key.startswith("crm."):
            assert "Changes recorded in CopilotGTM" in definition.description


@pytest.mark.parametrize("trigger_key", ["meeting.transcript.ready", "meeting.summary.ready"])
def test_meeting_catalog_uses_internal_user_and_business_option_sources(
    trigger_key: str,
) -> None:
    definition = _entries(build_trigger_catalog([]))[trigger_key]
    fields = {field.path: field for field in definition.fields}

    assert fields["record.provider"].option_source == "meeting_providers"
    assert fields["record.language"].option_source == "meeting_languages"
    assert fields["record.attendee_ids"].label == "Internal attendees"
    assert fields["record.attendee_ids"].option_source == "organisation_users"
    assert fields["record.owner_id"].label == "Organiser"
    assert fields["record.owner_id"].option_source == "organisation_users"
    assert fields["record.owner_id"].principal_modes == ["organisation"]
    assert fields["record.team_ids"].visibility == "hidden"


@pytest.mark.parametrize(
    ("trigger_key", "standard_paths"),
    [
        (
            "ai.deal_risk.detected",
            {"record.severity", "record.confidence", "record.deal_id"},
        ),
        (
            "ai.deal_risk.changed",
            {
                "record.status",
                "record.severity",
                "record.confidence",
                "record.deal_id",
            },
        ),
        (
            "ai.deal_risk.resolved",
            {
                "record.status",
                "record.severity",
                "record.confidence",
                "record.deal_id",
            },
        ),
        (
            "ai.deal_blocker.detected",
            {"record.severity", "record.confidence", "record.deal_id"},
        ),
        (
            "ai.deal_blocker.changed",
            {
                "record.status",
                "record.severity",
                "record.confidence",
                "record.deal_id",
            },
        ),
        (
            "ai.deal_blocker.resolved",
            {
                "record.status",
                "record.severity",
                "record.confidence",
                "record.deal_id",
            },
        ),
    ],
)
def test_ai_catalog_is_organisation_only_and_never_advertises_transition_ids(
    trigger_key: str,
    standard_paths: set[str],
) -> None:
    definition = _entries(build_trigger_catalog([]))[trigger_key]
    fields = {field.path: field for field in definition.fields}

    assert definition.principal_modes == ["organisation"]
    assert definition.source_selector.option_source == "ai_deals"
    assert {
        field.path for field in definition.fields if field.visibility == "standard"
    } == standard_paths
    assert fields["record.status"].option_source == "risk_statuses"
    assert fields["record.severity"].option_source == "risk_severities"
    assert fields["record.contact_id"].visibility == "hidden"
    assert not any(field.group in {"previous", "new"} for field in definition.fields)


def test_agent_run_catalog_uses_friendly_resolvable_conditions() -> None:
    entries = _entries(build_trigger_catalog([]))
    completed = {field.path: field for field in entries["agent.run.completed"].fields}
    failed = {field.path: field for field in entries["agent.run.failed"].fields}

    for fields in (completed, failed):
        assert fields["record.trigger_id"].label == "Triggering automation"
        assert fields["record.trigger_id"].option_source == "automation_triggers"
        assert fields["record.event_key"].option_source == "trigger_event_keys"
        assert fields["record.created_by_user_id"].option_source == "organisation_users"
        assert fields["record.created_by_user_id"].principal_modes == ["organisation"]
    assert completed["record.status"].visibility == "hidden"
    assert failed["record.status"].option_source == "agent_run_failure_statuses"

    enum_sets = build_trigger_catalog([]).option_sets.enums
    assert [item.label for item in enum_sets["agent_run_failure_statuses"]] == [
        "Execution failed",
        "Output validation failed",
    ]
    assert enum_sets["trigger_event_keys"]
