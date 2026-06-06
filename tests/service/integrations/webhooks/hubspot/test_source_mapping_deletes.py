from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm import CRMSourceObjectType
from src.service.integrations.webhooks.hubspot.companies import handler as company_handler
from src.service.integrations.webhooks.hubspot.contacts import handler as contact_handler
from src.service.integrations.webhooks.hubspot.deals import handler as deal_handler
from src.service.integrations.webhooks.hubspot.notes import handler as note_handler
from src.service.integrations.webhooks.hubspot.tasks import handler as task_handler


class _FakeExecuteResult:
    def __init__(self, row: object) -> None:
        self.row = row

    def scalars(self) -> SimpleNamespace:
        return SimpleNamespace(first=lambda: self.row)


class _FakeSession:
    def __init__(self, row: object) -> None:
        self.row = row
        self.statements: list[object] = []
        self.added: list[object] = []
        self.committed = False

    async def execute(self, statement):  # noqa: ANN001
        self.statements.append(statement)
        return _FakeExecuteResult(self.row)

    def add(self, row: object) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.committed = True


class _FakeMappingHelper:
    calls: list[dict[str, object]] = []
    local_id: UUID

    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        self.kwargs = kwargs

    async def mark_external_ids_deleted(self, *, object_type, external_ids):  # noqa: ANN001
        external_id = list(external_ids)[0]
        self.calls.append(
            {
                "provider": self.kwargs["provider"],
                "object_type": object_type,
                "external_id": external_id,
            }
        )
        return {external_id: self.local_id}


async def _acquire_session(session: _FakeSession):
    yield session


@pytest.mark.parametrize(
    ("module", "method_name", "row_factory", "object_type", "expected_column"),
    [
        (
            deal_handler,
            "_soft_delete_deal",
            lambda org_id, int_id, local_id: deal_handler.CRMDeal(
                organisation_id=org_id,
                integration_id=int_id,
                deal_id=local_id,
                external_id="provider-id",
            ),
            CRMSourceObjectType.DEAL,
            "crm_deals.deal_id",
        ),
        (
            company_handler,
            "_soft_delete_company",
            lambda org_id, int_id, local_id: company_handler.CRMCompany(
                organisation_id=org_id,
                integration_id=int_id,
                company_id=local_id,
                external_id="provider-id",
            ),
            CRMSourceObjectType.COMPANY,
            "crm_companies.company_id",
        ),
        (
            contact_handler,
            "_soft_delete_contact",
            lambda org_id, int_id, local_id: contact_handler.CRMContact(
                organisation_id=org_id,
                integration_id=int_id,
                contact_id=local_id,
                external_id="provider-id",
            ),
            CRMSourceObjectType.CONTACT,
            "crm_contacts.contact_id",
        ),
        (
            task_handler,
            "_soft_delete_task",
            lambda org_id, int_id, local_id: task_handler.CRMActivity(
                organisation_id=org_id,
                integration_id=int_id,
                activity_id=local_id,
                external_id="provider-id",
                activity_type=task_handler.CRMActivityType.TASK.value,
            ),
            CRMSourceObjectType.TASK,
            "crm_activities.activity_id",
        ),
        (
            note_handler,
            "_soft_delete_note",
            lambda org_id, int_id, local_id: note_handler.CRMActivity(
                organisation_id=org_id,
                integration_id=int_id,
                activity_id=local_id,
                external_id="provider-id",
                activity_type=note_handler.CRMActivityType.NOTE.value,
            ),
            CRMSourceObjectType.NOTE,
            "crm_activities.activity_id",
        ),
    ],
)
@pytest.mark.asyncio
async def test_hubspot_delete_helpers_resolve_mapping_before_local_soft_delete(
    monkeypatch: pytest.MonkeyPatch,
    module,
    method_name: str,
    row_factory,
    object_type: CRMSourceObjectType,
    expected_column: str,
) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    local_id = uuid4()
    _FakeMappingHelper.calls = []
    _FakeMappingHelper.local_id = local_id
    row = row_factory(organisation_id, integration_id, local_id)
    session = _FakeSession(row)

    monkeypatch.setattr(module, "CRMSourceMappingHelper", _FakeMappingHelper)
    monkeypatch.setattr(
        module, "acquire_org_async_session", lambda _org_id: _acquire_session(session)
    )

    result = (
        await getattr(module.HubSpotDealWebhookHandler, method_name)(
            organisation_id=organisation_id,
            integration_id=integration_id,
            external_id="provider-id",
        )
        if module is deal_handler
        else await getattr(
            module.HubSpotCompanyWebhookHandler
            if module is company_handler
            else module.HubSpotContactWebhookHandler
            if module is contact_handler
            else module.HubSpotTaskWebhookHandler
            if module is task_handler
            else module.HubSpotNoteWebhookHandler,
            method_name,
        )(
            organisation_id=organisation_id,
            integration_id=integration_id,
            external_id="provider-id",
        )
    )

    sql_text = str(session.statements[0])
    where_clause = sql_text.partition("WHERE")[2]
    assert result is True
    assert _FakeMappingHelper.calls == [
        {
            "provider": "hubspot",
            "object_type": object_type,
            "external_id": "provider-id",
        }
    ]
    assert expected_column in sql_text
    assert ".external_id" not in where_clause
    assert row.is_deleted is True
    assert session.committed is True
