from uuid import UUID

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.integrations.webhooks.microsoft.metadata import MicrosoftSubscriptionKind
from src.service.integrations.webhooks.microsoft.subscription_task_service import (
    MicrosoftGraphSubscriptionTaskService,
)
from src.workers.integrations_worker.tasks.microsoft_graph_subscription_tasks import (
    _kind_for_integration_type,
)


def _integration(
    *,
    integration_id: UUID,
    organisation_id: UUID = UUID("11111111-1111-1111-1111-111111111111"),
    integration_type: IntegrationType = IntegrationType.OUTLOOK_EMAIL,
    connection_id: str = "conn",
) -> KnowledgeIntegration:
    return KnowledgeIntegration.model_validate(
        {
            "integration_id": integration_id,
            "organisation_id": organisation_id,
            "created_by_user_id": UUID("33333333-3333-3333-3333-333333333333"),
            "integration_type": integration_type,
            "name": integration_type.value,
            "credentials": {},
            "connection_id": connection_id,
            "is_active": True,
            "connection_metadata": {},
        }
    )


def test_kind_for_integration_type_maps_only_outlook_v1_types() -> None:
    assert (
        _kind_for_integration_type(IntegrationType.OUTLOOK_CALENDAR)
        == MicrosoftSubscriptionKind.CALENDAR
    )
    assert (
        _kind_for_integration_type(IntegrationType.OUTLOOK_EMAIL) == MicrosoftSubscriptionKind.EMAIL
    )
    assert _kind_for_integration_type(IntegrationType.TEAMS) is None


@pytest.mark.asyncio
async def test_ensure_service_updates_subscription_metadata(monkeypatch) -> None:
    from src.service.integrations.webhooks.microsoft import subscription_task_service

    integration = _integration(integration_id=UUID("22222222-2222-2222-2222-222222222222"))

    class FakeResult:
        def scalar_one_or_none(self):
            return integration

    class FakeSession:
        async def execute(self, statement):
            return FakeResult()

        def add(self, instance):
            assert instance is integration

        async def commit(self):
            integration.connection_metadata["committed"] = True

    async def fake_session_generator(org_id):
        yield FakeSession()

    async def fake_ensure(self, *, organisation_id, integration, kind):
        return {"microsoft_graph_subscriptions": {"email": {"subscription_id": "sub"}}}

    monkeypatch.setattr(
        subscription_task_service,
        "acquire_org_async_session",
        fake_session_generator,
    )
    monkeypatch.setattr(
        subscription_task_service.MicrosoftGraphSubscriptionService,
        "ensure_subscription_metadata",
        fake_ensure,
    )

    result = await MicrosoftGraphSubscriptionTaskService().ensure_subscription(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        integration_id=UUID("22222222-2222-2222-2222-222222222222"),
    )

    assert result["status"] == "success"
    assert result["kind"] == "email"
    assert integration.connection_metadata["committed"] is True


@pytest.mark.asyncio
async def test_renew_all_dispatches_integration_level_ensure_tasks(monkeypatch) -> None:
    from src.service.integrations.webhooks.microsoft import subscription_task_service

    first_org_id = UUID("11111111-1111-1111-1111-111111111111")
    second_org_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    first_integration_id = UUID("22222222-2222-2222-2222-222222222222")
    second_integration_id = UUID("44444444-4444-4444-4444-444444444444")
    dispatched: list[tuple[UUID, UUID]] = []

    class ScalarResult:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return self

        def all(self):
            return self.values

    class FakeSession:
        def __init__(self, values):
            self.values = values

        async def execute(self, statement):
            return ScalarResult(self.values)

    async def fake_master_session_generator():
        yield FakeSession([first_org_id, second_org_id])

    async def fake_org_session_generator(org_id):
        if org_id == first_org_id:
            yield FakeSession([first_integration_id])
            return
        yield FakeSession([second_integration_id])

    monkeypatch.setattr(
        subscription_task_service,
        "acquire_master_async_session",
        fake_master_session_generator,
    )
    monkeypatch.setattr(
        subscription_task_service,
        "acquire_org_async_session",
        fake_org_session_generator,
    )

    result = await MicrosoftGraphSubscriptionTaskService().dispatch_all_subscription_ensures(
        dispatch_subscription_ensure=lambda org_id, int_id: dispatched.append((org_id, int_id)),
    )

    assert result["status"] == "success"
    assert result["orgs_checked"] == 2
    assert result["integrations_dispatched"] == 2
    assert dispatched == [
        (first_org_id, first_integration_id),
        (second_org_id, second_integration_id),
    ]
