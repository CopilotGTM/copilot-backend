"""Authority and tenant boundaries for trigger option values."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from src.domain.models.master_tables import AdminUserStatus, SupabaseUserStatus
from src.domain.models.organisation_tables.agent_models import AutomationTriggerStatus
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.automation.triggers.direct import ManualInvocationAccess, ManualSubject
from src.service.automation.triggers.options import AutomationTriggerOptionAccess


class ScalarResult:
    def __init__(self, values: list[Any]) -> None:
        self.values = values

    def scalars(self) -> ScalarResult:
        return self

    def all(self) -> list[Any]:
        return self.values


class RecordingSession:
    def __init__(self, results: list[list[Any]]) -> None:
        self.results = iter(results)
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> ScalarResult:
        self.statements.append(statement)
        return ScalarResult(next(self.results))


def sql(statement: Any) -> str:
    return " ".join(str(statement.compile(dialect=postgresql.dialect())).lower().split())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "identity_column", "personal_fragments"),
    [
        (
            "deals",
            "crm_deals.deal_id",
            ("crm_deals.owner_id", "crm_deals.created_by_user_id", " or "),
        ),
        ("contacts", "crm_contacts.contact_id", ("crm_contacts.owner_id",)),
        ("accounts", "crm_companies.company_id", ("crm_companies.owner_id",)),
    ],
)
async def test_crm_source_selection_uses_the_execution_authority(
    source: str,
    identity_column: str,
    personal_fragments: tuple[str, ...],
) -> None:
    organisation_id, principal_id, record_id = uuid4(), uuid4(), uuid4()
    personal_session = RecordingSession([[record_id]])

    await AutomationTriggerOptionAccess(
        personal_session,  # type: ignore[arg-type]
        organisation_id,
    ).validate_source_selection(
        source,
        {str(record_id)},
        principal_id=principal_id,
        organisation_scope=False,
    )

    personal_sql = sql(personal_session.statements[0])
    assert identity_column in personal_sql
    assert "organisation_id" in personal_sql
    assert "is_deleted is false" in personal_sql
    assert "crm_users.user_id" in personal_sql
    assert "public.users.email" in personal_sql
    assert all(fragment in personal_sql for fragment in personal_fragments)

    organisation_session = RecordingSession([[record_id]])
    await AutomationTriggerOptionAccess(
        organisation_session,  # type: ignore[arg-type]
        organisation_id,
    ).validate_source_selection(
        source,
        {str(record_id)},
        principal_id=organisation_id,
        organisation_scope=True,
    )

    organisation_sql = sql(organisation_session.statements[0])
    assert "owner_id" not in organisation_sql
    assert "created_by_user_id" not in organisation_sql


@pytest.mark.asyncio
async def test_mailbox_source_selection_requires_active_email_authority() -> None:
    organisation_id, principal_id, mailbox_id = uuid4(), uuid4(), uuid4()
    session = RecordingSession([[mailbox_id]])

    await AutomationTriggerOptionAccess(
        session,  # type: ignore[arg-type]
        organisation_id,
    ).validate_source_selection(
        "email_mailboxes",
        {str(mailbox_id)},
        principal_id=principal_id,
        organisation_scope=False,
    )

    statement = sql(session.statements[0])
    assert "knowledge_integrations.organisation_id" in statement
    assert "knowledge_integrations.created_by_user_id" in statement
    assert "knowledge_integrations.is_active is true" in statement
    assert "knowledge_integrations.integration_type in" in statement


@pytest.mark.asyncio
async def test_agent_source_selection_requires_active_accessible_agent() -> None:
    organisation_id, principal_id, agent_id = uuid4(), uuid4(), uuid4()
    session = RecordingSession([[agent_id]])

    await AutomationTriggerOptionAccess(
        session,  # type: ignore[arg-type]
        organisation_id,
    ).validate_source_selection(
        "agents",
        {str(agent_id)},
        principal_id=principal_id,
        organisation_scope=False,
    )

    statement = sql(session.statements[0])
    assert "user_agents.organisation_id" in statement
    assert "user_agents.owner_user_id" in statement
    assert "user_agents.status =" in statement


@pytest.mark.asyncio
async def test_source_selection_rejects_foreign_inactive_or_missing_values() -> None:
    session = RecordingSession([[]])

    with pytest.raises(ValueError, match="Unknown or unavailable"):
        await AutomationTriggerOptionAccess(
            session,  # type: ignore[arg-type]
            uuid4(),
        ).validate_source_selection(
            "deals",
            {str(uuid4())},
            principal_id=uuid4(),
            organisation_scope=False,
        )


@pytest.mark.asyncio
async def test_manual_and_schedule_record_access_use_the_same_crm_identity_mapping() -> None:
    session = RecordingSession([[]])

    await ManualInvocationAccess(
        session,  # type: ignore[arg-type]
        uuid4(),
    ).load_subject_records(
        (ManualSubject("deal", uuid4()),),
        principal_id=uuid4(),
        organisation_scope=False,
    )

    statement = sql(session.statements[0])
    assert "crm_users.user_id" in statement
    assert "public.users.email" in statement
    assert "crm_deals.owner_id in" in statement
    assert "crm_deals.created_by_user_id in" in statement


@pytest.mark.asyncio
async def test_mailbox_search_returns_friendly_labels_and_cursor() -> None:
    organisation_id, principal_id = uuid4(), uuid4()
    rows = [
        KnowledgeIntegration(
            organisation_id=organisation_id,
            created_by_user_id=principal_id,
            integration_type=IntegrationType.GOOGLE,
            integration_email="sales@example.com",
            name="Sales",
            credentials={},
        ),
        KnowledgeIntegration(
            organisation_id=organisation_id,
            created_by_user_id=principal_id,
            integration_type=IntegrationType.MICROSOFT,
            integration_email="renewals@example.com",
            name="Renewals",
            credentials={},
        ),
    ]
    session = RecordingSession([rows])

    items, next_offset = await AutomationTriggerOptionAccess(
        session,  # type: ignore[arg-type]
        organisation_id,
    ).search_options(
        "email_mailboxes",
        query="sales",
        limit=1,
        offset=0,
        principal_id=principal_id,
        organisation_scope=False,
        authority_scoped=True,
    )

    assert items[0].label == "sales@example.com · Google"
    assert next_offset == 1
    statement = sql(session.statements[0])
    assert "knowledge_integrations.created_by_user_id" in statement
    assert "knowledge_integrations.is_active is true" in statement
    assert "lower(coalesce(knowledge_integrations.integration_email" in statement


@pytest.mark.asyncio
async def test_resolution_preserves_tenant_inactive_status_for_service_disclosure_gate() -> None:
    organisation_id, principal_id = uuid4(), uuid4()
    mailbox = KnowledgeIntegration(
        organisation_id=organisation_id,
        created_by_user_id=principal_id,
        integration_type=IntegrationType.MICROSOFT,
        integration_email="old@example.com",
        name="Old mailbox",
        credentials={},
        is_active=False,
    )
    session = RecordingSession([[mailbox]])

    resolved = await AutomationTriggerOptionAccess(
        session,  # type: ignore[arg-type]
        organisation_id,
    ).resolve_options(
        "email_mailboxes",
        {str(mailbox.integration_id)},
        principal_id=principal_id,
        organisation_scope=False,
        authority_scoped=True,
    )

    item = resolved[str(mailbox.integration_id)]
    assert item.label == "old@example.com · Microsoft"
    assert item.status == "inactive"
    assert item.selectable is False


@pytest.mark.asyncio
async def test_ai_deal_source_is_explicitly_organisation_only() -> None:
    access = AutomationTriggerOptionAccess(RecordingSession([]), uuid4())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="organisation only"):
        await access.validate_source_selection(
            "ai_deals",
            {str(uuid4())},
            principal_id=uuid4(),
            organisation_scope=False,
        )


@pytest.mark.asyncio
async def test_static_business_option_sources_validate_and_search_friendly_labels() -> None:
    access = AutomationTriggerOptionAccess(RecordingSession([]), uuid4())  # type: ignore[arg-type]

    await access.validate_option_selection("risk_severities", {"HIGH", "CRITICAL"})
    with pytest.raises(ValueError, match="Unknown risk_severities"):
        await access.validate_option_selection("risk_severities", {"SEVERE"})

    items, cursor = await access.search_options(
        "agent_run_failure_statuses",
        query="delivery",
        limit=25,
        offset=0,
        principal_id=uuid4(),
        organisation_scope=True,
        authority_scoped=False,
    )

    assert [(item.value, item.label) for item in items] == [("delivery_failed", "Delivery failed")]
    assert cursor is None


@pytest.mark.asyncio
async def test_meeting_language_options_are_distinct_tenant_values() -> None:
    session = RecordingSession([["en-US", "fr-FR"]])

    items, cursor = await AutomationTriggerOptionAccess(
        session,  # type: ignore[arg-type]
        uuid4(),
    ).search_options(
        "meeting_languages",
        query="",
        limit=25,
        offset=0,
        principal_id=uuid4(),
        organisation_scope=False,
        authority_scoped=False,
    )

    assert [item.label for item in items] == ["en-US", "fr-FR"]
    assert cursor is None
    assert "meetings.organisation_id" in sql(session.statements[0])
    assert "transcript_language is not null" in sql(session.statements[0])


@pytest.mark.asyncio
async def test_workspace_users_and_saved_triggers_resolve_to_business_labels() -> None:
    user_id, trigger_id, organisation_id = uuid4(), uuid4(), uuid4()
    user = SimpleNamespace(
        user_id=user_id,
        first_name="Alice",
        last_name="Buyer",
        email="alice@example.com",
        status=SupabaseUserStatus.ACTIVE,
        admin_status=AdminUserStatus.APPROVED,
    )
    trigger = SimpleNamespace(
        trigger_id=trigger_id,
        trigger_key="crm.deal.updated",
        name="Review important changes",
        status=AutomationTriggerStatus.ACTIVE,
    )
    session = RecordingSession([[user], [trigger]])
    access = AutomationTriggerOptionAccess(session, organisation_id)  # type: ignore[arg-type]

    users, _ = await access.search_options(
        "organisation_users",
        query="alice",
        limit=25,
        offset=0,
        principal_id=user_id,
        organisation_scope=True,
        authority_scoped=False,
    )
    triggers, _ = await access.search_options(
        "automation_triggers",
        query="review",
        limit=25,
        offset=0,
        principal_id=user_id,
        organisation_scope=False,
        authority_scoped=False,
    )

    assert users[0].label == "Alice Buyer · alice@example.com"
    assert triggers[0].label == "CRM / Deal / Updated · Review important changes"
    assert "public.users.organisation_id" in sql(session.statements[0])
    assert "automation_triggers.created_by_user_id" in sql(session.statements[1])
