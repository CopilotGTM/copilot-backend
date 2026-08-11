from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm import (
    CRMCompany,
    CRMContact,
    CRMDeal,
    CRMNote,
    CRMSourceObjectType,
    CRMTask,
    CRMUser,
)
from src.service.integrations.webhooks.salesforce import (
    change_processor as salesforce_cdc_subscriber_task,
)
from src.workers.integrations_worker.providers.crm.salesforce.pubsub_client import (
    SalesforcePubSubReplayIdError,
)
from src.workers.integrations_worker.tasks.crm.salesforce import (
    salesforce_cdc_subscriber_task as subscriber_task,
)


def _run(coro):
    return asyncio.run(coro)


async def _acquire_session(_organisation_id):
    yield AsyncMock()


class _FakeDeleteExecuteResult:
    def __init__(self, row: object) -> None:
        self.row = row

    def scalar_one_or_none(self) -> object:
        return self.row

    def scalars(self) -> _FakeDeleteExecuteResult:
        return self

    def all(self) -> list[object]:
        return []


class _FakeDeleteSession:
    def __init__(self, row: object) -> None:
        self.row = row
        self.statements: list[object] = []
        self.committed = False

    async def execute(self, statement):  # noqa: ANN001
        self.statements.append(statement)
        return _FakeDeleteExecuteResult(self.row)

    def add(self, _row) -> None:  # noqa: ANN001
        return None

    async def commit(self) -> None:
        self.committed = True


class _FakeDeleteMappingHelper:
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


class _FakeActivityFeedProjectionService:
    def __init__(self, *_args: object) -> None:
        pass

    async def soft_delete_activity_projection(self, _activity_id: UUID) -> None:
        pass


async def _acquire_delete_session(session: _FakeDeleteSession):
    yield session


def test_completed_salesforce_cdc_run_releases_lease_before_requeue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    released: list[dict[str, object]] = []
    queued: list[dict[str, object]] = []

    _patch_context(monkeypatch, org_id, integration_id)

    class FakePubSubClient:
        def __init__(self, **_kwargs):
            pass

        async def subscribe(self, **_kwargs):
            if False:
                yield None

    class FakeStateService(_BaseStateService):
        async def release_lease(self, **kwargs):
            released.append(kwargs)
            return True

    async def fake_send_integrations_task_async(**kwargs):
        queued.append(kwargs)
        return "task-id"

    monkeypatch.setattr(subscriber_task, "SalesforcePubSubClient", FakePubSubClient)
    monkeypatch.setattr(
        subscriber_task,
        "SalesforceCdcSubscriptionStateService",
        FakeStateService,
    )
    monkeypatch.setattr(
        subscriber_task,
        "send_integrations_task_async",
        fake_send_integrations_task_async,
    )

    result = _run(
        subscriber_task._run_subscriber(
            organisation_id=org_id,
            integration_id=integration_id,
            channel="/data/OpportunityChangeEvent",
        )
    )

    assert result["status"] == "completed"
    assert len(released) == 1
    assert released[0]["organisation_id"] == org_id
    assert queued[0]["task_name"] == "integrations.run_salesforce_cdc_subscriber"


def test_salesforce_cdc_token_failure_releases_lease_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    released: list[dict[str, object]] = []
    queued: list[dict[str, object]] = []

    _patch_context(monkeypatch, org_id, integration_id)

    class FakeStateService(_BaseStateService):
        async def release_lease(self, **kwargs):
            released.append(kwargs)
            return True

    async def fake_get_access_token(_token_info):
        raise RuntimeError("token refresh failed")

    async def fake_send_integrations_task_async(**kwargs):
        queued.append(kwargs)
        return "task-id"

    monkeypatch.setattr(subscriber_task, "get_access_token", fake_get_access_token)
    monkeypatch.setattr(
        subscriber_task,
        "SalesforceCdcSubscriptionStateService",
        FakeStateService,
    )
    monkeypatch.setattr(
        subscriber_task,
        "send_integrations_task_async",
        fake_send_integrations_task_async,
    )

    with pytest.raises(RuntimeError, match="token refresh failed"):
        _run(
            subscriber_task._run_subscriber(
                organisation_id=org_id,
                integration_id=integration_id,
                channel="/data/OpportunityChangeEvent",
            )
        )

    assert len(released) == 1
    assert released[0]["integration_id"] == integration_id
    assert queued[0]["countdown"] == 60


def test_salesforce_cdc_replay_repair_releases_lease_before_repair_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    operations: list[str] = []

    _patch_context(monkeypatch, org_id, integration_id)

    class FakePubSubClient:
        def __init__(self, **_kwargs):
            pass

        async def subscribe(self, **_kwargs):
            raise SalesforcePubSubReplayIdError("replay ID outside retention")
            if False:
                yield None

    class FakeStateService(_BaseStateService):
        async def acquire_lease(self, **_kwargs):
            return SimpleNamespace(
                acquired=True, subscription=SimpleNamespace(last_replay_id="AAE=")
            )

        async def clear_replay_for_repair(self, **_kwargs):
            operations.append("clear")

        async def release_lease(self, **_kwargs):
            operations.append("release")
            return True

    async def fake_send_integrations_task_async(*_args, **_kwargs):
        operations.append("enqueue")
        return "task-id"

    monkeypatch.setattr(subscriber_task, "SalesforcePubSubClient", FakePubSubClient)
    monkeypatch.setattr(
        subscriber_task,
        "SalesforceCdcSubscriptionStateService",
        FakeStateService,
    )
    monkeypatch.setattr(
        subscriber_task,
        "send_integrations_task_async",
        fake_send_integrations_task_async,
    )

    result = _run(
        subscriber_task._run_subscriber(
            organisation_id=org_id,
            integration_id=integration_id,
            channel="/data/OpportunityChangeEvent",
        )
    )

    assert result["status"] == "replay_expired_repair_queued"
    assert operations == ["clear", "release", "enqueue"]


def test_legacy_subscriber_exits_without_requeue_in_aggregate_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    queued: list[dict[str, object]] = []

    async def fake_load_integration(**_kwargs):
        return SimpleNamespace(connection_metadata={"salesforce_cdc": {"mode": "aggregate"}})

    async def fake_send(**kwargs):
        queued.append(kwargs)

    monkeypatch.setattr(subscriber_task, "_load_integration", fake_load_integration)
    monkeypatch.setattr(subscriber_task, "acquire_org_async_session", _acquire_session)
    monkeypatch.setattr(subscriber_task, "send_integrations_task_async", fake_send)

    result = _run(
        subscriber_task._run_subscriber(
            organisation_id=org_id,
            integration_id=integration_id,
            channel="/data/OpportunityChangeEvent",
        )
    )

    assert result == {"status": "superseded", "events_seen": 0, "records_processed": 0}
    assert queued == []


@pytest.mark.asyncio
async def test_opportunity_cdc_create_enables_deal_created_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    persisted: list[dict[str, object]] = []

    class FakeOpportunityFetcher:
        def __init__(self, *, token_info, instance_url, opportunity_id):  # noqa: ANN001
            assert token_info == "token-info"
            assert instance_url == "https://acme.my.salesforce.com"
            assert opportunity_id == "006xx0000000001AAA"

        async def fetch(self):
            return {"Id": "006xx0000000001AAA", "Name": "Acme renewal"}

    class FakeDealService:
        async def _load_context(self, organisation_id, integration_id):  # noqa: ANN001
            return SimpleNamespace(token_info="token-info")

        async def persist_single_opportunity(self, **kwargs):
            persisted.append(kwargs)

    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "SalesforceSingleOpportunityFetcher",
        FakeOpportunityFetcher,
    )
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "SalesforceDealRecordSyncService",
        FakeDealService,
    )

    await salesforce_cdc_subscriber_task._process_record_change(
        organisation_id=org_id,
        integration_id=integration_id,
        record_id="006xx0000000001AAA",
        change_type="CREATE",
        instance_url="https://acme.my.salesforce.com",
        channel="/data/OpportunityChangeEvent",
    )

    assert persisted[0]["raw_opportunity"]["Id"] == "006xx0000000001AAA"
    assert persisted[0]["project_deal_created_activity_feed"] is True


@pytest.mark.asyncio
async def test_opportunity_cdc_update_does_not_enable_deal_created_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    persisted: list[dict[str, object]] = []

    class FakeOpportunityFetcher:
        def __init__(self, **_kwargs) -> None:
            pass

        async def fetch(self):
            return {"Id": "006xx0000000001AAA", "Name": "Acme renewal"}

    class FakeDealService:
        async def _load_context(self, organisation_id, integration_id):  # noqa: ANN001
            return SimpleNamespace(token_info="token-info")

        async def persist_single_opportunity(self, **kwargs):
            persisted.append(kwargs)

    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "SalesforceSingleOpportunityFetcher",
        FakeOpportunityFetcher,
    )
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "SalesforceDealRecordSyncService",
        FakeDealService,
    )

    await salesforce_cdc_subscriber_task._process_record_change(
        organisation_id=org_id,
        integration_id=integration_id,
        record_id="006xx0000000001AAA",
        change_type="UPDATE",
        instance_url="https://acme.my.salesforce.com",
        channel="/data/OpportunityChangeEvent",
    )

    assert persisted[0]["project_deal_created_activity_feed"] is False


@pytest.mark.parametrize(
    (
        "function_name",
        "id_kwarg",
        "row_factory",
        "object_type",
        "expected_column",
        "expected_state",
    ),
    [
        (
            "_soft_delete_deal",
            "external_id",
            lambda org_id, int_id, local_id: CRMDeal(
                organisation_id=org_id,
                integration_id=int_id,
                deal_id=local_id,
                external_id="provider-id",
            ),
            CRMSourceObjectType.DEAL,
            "crm_deals.deal_id",
            ("is_deleted", True),
        ),
        (
            "_deactivate_company",
            "external_id",
            lambda org_id, int_id, local_id: CRMCompany(
                organisation_id=org_id,
                integration_id=int_id,
                company_id=local_id,
                external_id="provider-id",
                is_active=True,
            ),
            CRMSourceObjectType.COMPANY,
            "crm_companies.company_id",
            ("is_active", False),
        ),
        (
            "_soft_delete_contact",
            "external_id",
            lambda org_id, int_id, local_id: CRMContact(
                organisation_id=org_id,
                integration_id=int_id,
                contact_id=local_id,
                external_id="provider-id",
            ),
            CRMSourceObjectType.CONTACT,
            "crm_contacts.contact_id",
            ("is_deleted", True),
        ),
        (
            "_soft_delete_task",
            "external_id",
            lambda org_id, int_id, local_id: CRMTask(
                organisation_id=org_id,
                integration_id=int_id,
                task_id=local_id,
                title="Follow up",
            ),
            CRMSourceObjectType.TASK,
            "crm_tasks.task_id",
            ("is_deleted", True),
        ),
        (
            "_deactivate_user",
            "external_id",
            lambda org_id, int_id, local_id: CRMUser(
                organisation_id=org_id,
                integration_id=int_id,
                user_id=local_id,
                external_id="provider-id",
                is_active=True,
            ),
            CRMSourceObjectType.USER,
            "crm_users.user_id",
            ("is_active", False),
        ),
        (
            "_soft_delete_note",
            "external_id",
            lambda org_id, int_id, local_id: CRMNote(
                organisation_id=org_id,
                integration_id=int_id,
                note_id=local_id,
                title="QBR note",
                body="Discussed renewal.",
            ),
            CRMSourceObjectType.NOTE,
            "crm_notes.note_id",
            ("is_deleted", True),
        ),
        (
            "_soft_delete_note_by_content_document_link_id",
            "content_document_link_id",
            lambda org_id, int_id, local_id: CRMNote(
                organisation_id=org_id,
                integration_id=int_id,
                note_id=local_id,
                title="QBR note",
                body="Discussed renewal.",
            ),
            CRMSourceObjectType.NOTE_LINK,
            "crm_notes.note_id",
            ("is_deleted", True),
        ),
    ],
)
@pytest.mark.asyncio
async def test_salesforce_cdc_delete_helpers_resolve_source_mapping_before_local_update(
    monkeypatch: pytest.MonkeyPatch,
    function_name: str,
    id_kwarg: str,
    row_factory,
    object_type: CRMSourceObjectType,
    expected_column: str,
    expected_state: tuple[str, object],
) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    local_id = uuid4()
    row = row_factory(organisation_id, integration_id, local_id)
    session = _FakeDeleteSession(row)
    _FakeDeleteMappingHelper.calls = []
    _FakeDeleteMappingHelper.local_id = local_id

    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "CRMSourceMappingHelper",
        _FakeDeleteMappingHelper,
    )
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "acquire_org_async_session",
        lambda _org_id: _acquire_delete_session(session),
    )
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "ActivityFeedProjectionService",
        _FakeActivityFeedProjectionService,
    )

    await getattr(salesforce_cdc_subscriber_task, function_name)(
        organisation_id=organisation_id,
        integration_id=integration_id,
        **{id_kwarg: "provider-id"},
    )

    where_clause = str(session.statements[0]).partition("WHERE")[2]
    state_attr, state_value = expected_state
    assert _FakeDeleteMappingHelper.calls == [
        {
            "provider": "salesforce",
            "object_type": object_type,
            "external_id": "provider-id",
        }
    ]
    assert expected_column in where_clause
    assert ".external_id" not in where_clause
    assert getattr(row, state_attr) is state_value
    assert session.committed is True


@pytest.mark.asyncio
async def test_deleted_source_id_resolution_falls_back_to_already_deleted_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    local_id = uuid4()
    calls: list[dict[str, object]] = []

    class FakeMappingHelper:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            self.kwargs = kwargs

        async def mark_external_ids_deleted(self, *, object_type, external_ids):  # noqa: ANN001
            calls.append(
                {
                    "method": "mark",
                    "object_type": object_type,
                    "external_ids": list(external_ids),
                    "provider": self.kwargs["provider"],
                }
            )
            return {}

        async def load_local_id_map(
            self,
            *,
            object_type,
            external_ids,
            include_deleted,
        ):  # noqa: ANN001
            calls.append(
                {
                    "method": "load",
                    "object_type": object_type,
                    "external_ids": list(external_ids),
                    "include_deleted": include_deleted,
                    "provider": self.kwargs["provider"],
                }
            )
            return {"provider-id": local_id}

    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "CRMSourceMappingHelper",
        FakeMappingHelper,
    )

    resolved = await salesforce_cdc_subscriber_task._resolve_deleted_source_id(
        session=AsyncMock(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        object_type=CRMSourceObjectType.TASK,
        external_id="provider-id",
    )

    assert resolved == local_id
    assert calls == [
        {
            "method": "mark",
            "object_type": CRMSourceObjectType.TASK,
            "external_ids": ["provider-id"],
            "provider": "salesforce",
        },
        {
            "method": "load",
            "object_type": CRMSourceObjectType.TASK,
            "external_ids": ["provider-id"],
            "include_deleted": True,
            "provider": "salesforce",
        },
    ]


@pytest.mark.parametrize(
    ("function_name", "id_kwarg", "object_type", "projection_column"),
    [
        (
            "_soft_delete_task",
            "external_id",
            CRMSourceObjectType.TASK,
            "crm_activities.activity_id",
        ),
        (
            "_soft_delete_note",
            "external_id",
            CRMSourceObjectType.NOTE,
            "crm_activities.activity_id",
        ),
        (
            "_soft_delete_note_by_content_document_link_id",
            "content_document_link_id",
            CRMSourceObjectType.NOTE_LINK,
            "crm_activities.activity_id",
        ),
    ],
)
@pytest.mark.asyncio
async def test_salesforce_cdc_delete_helpers_tombstone_projection_when_source_row_missing(
    monkeypatch: pytest.MonkeyPatch,
    function_name: str,
    id_kwarg: str,
    object_type: CRMSourceObjectType,
    projection_column: str,
) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    local_id = uuid4()
    session = _FakeDeleteSession(None)
    _FakeDeleteMappingHelper.calls = []
    _FakeDeleteMappingHelper.local_id = local_id

    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "CRMSourceMappingHelper",
        _FakeDeleteMappingHelper,
    )
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "acquire_org_async_session",
        lambda _org_id: _acquire_delete_session(session),
    )

    await getattr(salesforce_cdc_subscriber_task, function_name)(
        organisation_id=organisation_id,
        integration_id=integration_id,
        **{id_kwarg: "provider-id"},
    )

    assert _FakeDeleteMappingHelper.calls == [
        {
            "provider": "salesforce",
            "object_type": object_type,
            "external_id": "provider-id",
        }
    ]
    assert any(projection_column in str(statement) for statement in session.statements)
    assert session.committed is True


@pytest.mark.asyncio
async def test_content_document_link_delete_soft_deletes_note_by_document_id_when_no_links_remain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    deleted: list[str] = []

    class FakeNotesFetcher:
        def __init__(self, *, token_info, instance_url):  # noqa: ANN001
            pass

        async def fetch_one_by_link_id(self, link_id: str, *, linked_entity_ids):
            assert link_id == "06Axx0000000001AAA"
            assert linked_entity_ids == ["006xx0000000001AAA"]
            return []

        async def fetch_link_by_id(self, link_id: str):
            assert link_id == "06Axx0000000001AAA"
            return {
                "Id": link_id,
                "ContentDocumentId": "069xx0000000001AAA",
                "LinkedEntityId": "006xx0000000001AAA",
            }

        async def fetch_one_by_note_id(self, note_id: str, *, linked_entity_ids):
            assert note_id == "069xx0000000001AAA"
            assert linked_entity_ids == ["006xx0000000001AAA"]
            return []

    class FakeNoteService:
        async def _load_context(self, organisation_id, integration_id):  # noqa: ANN001
            return SimpleNamespace(token_info="token-info")

        async def _load_linked_entity_ids(self, organisation_id, integration_id):  # noqa: ANN001
            return ["006xx0000000001AAA"]

    async def fake_soft_delete_note(**kwargs):
        deleted.append(kwargs["external_id"])

    async def fail_soft_delete_by_link_id(**_kwargs):
        raise AssertionError("document ID delete path should be preferred")

    monkeypatch.setattr(salesforce_cdc_subscriber_task, "SalesforceNotesFetcher", FakeNotesFetcher)
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "SalesforceNoteRecordSyncService",
        FakeNoteService,
    )
    monkeypatch.setattr(salesforce_cdc_subscriber_task, "_soft_delete_note", fake_soft_delete_note)
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "_soft_delete_note_by_content_document_link_id",
        fail_soft_delete_by_link_id,
    )

    await salesforce_cdc_subscriber_task._process_record_change(
        organisation_id=org_id,
        integration_id=integration_id,
        record_id="06Axx0000000001AAA",
        change_type="DELETE",
        instance_url="https://acme.my.salesforce.com",
        channel="/data/ContentDocumentLinkChangeEvent",
    )

    assert deleted == ["069xx0000000001AAA"]


@pytest.mark.asyncio
async def test_content_document_link_delete_persists_note_when_another_scoped_link_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    persisted: list[list[dict[str, str]]] = []

    class FakeNotesFetcher:
        def __init__(self, *, token_info, instance_url):  # noqa: ANN001
            pass

        async def fetch_one_by_link_id(self, link_id: str, *, linked_entity_ids):
            assert link_id == "06Axx0000000001AAA"
            assert linked_entity_ids == ["006xx0000000001AAA", "003xx0000000001AAA"]
            return []

        async def fetch_link_by_id(self, link_id: str):
            assert link_id == "06Axx0000000001AAA"
            return {
                "Id": link_id,
                "ContentDocumentId": "069xx0000000001AAA",
                "LinkedEntityId": "006xx0000000001AAA",
            }

        async def fetch_one_by_note_id(self, note_id: str, *, linked_entity_ids):
            assert note_id == "069xx0000000001AAA"
            assert linked_entity_ids == ["006xx0000000001AAA", "003xx0000000001AAA"]
            return [
                {
                    "Id": "06Axx0000000002AAA",
                    "ContentDocumentId": note_id,
                    "LinkedEntityId": "003xx0000000001AAA",
                }
            ]

    class FakeNoteService:
        async def _load_context(self, organisation_id, integration_id):  # noqa: ANN001
            return SimpleNamespace(token_info="token-info")

        async def _load_linked_entity_ids(self, organisation_id, integration_id):  # noqa: ANN001
            return ["006xx0000000001AAA", "003xx0000000001AAA"]

        async def persist_single_note(self, **kwargs):
            persisted.append(kwargs["raw_notes"])

    async def fail_delete(**_kwargs):
        raise AssertionError("remaining scoped links must keep the note active")

    monkeypatch.setattr(salesforce_cdc_subscriber_task, "SalesforceNotesFetcher", FakeNotesFetcher)
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "SalesforceNoteRecordSyncService",
        FakeNoteService,
    )
    monkeypatch.setattr(salesforce_cdc_subscriber_task, "_soft_delete_note", fail_delete)
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "_soft_delete_note_by_content_document_link_id",
        fail_delete,
    )

    await salesforce_cdc_subscriber_task._process_record_change(
        organisation_id=org_id,
        integration_id=integration_id,
        record_id="06Axx0000000001AAA",
        change_type="DELETE",
        instance_url="https://acme.my.salesforce.com",
        channel="/data/ContentDocumentLinkChangeEvent",
    )

    assert persisted == [
        [
            {
                "Id": "06Axx0000000002AAA",
                "ContentDocumentId": "069xx0000000001AAA",
                "LinkedEntityId": "003xx0000000001AAA",
            }
        ]
    ]


@pytest.mark.asyncio
async def test_content_document_link_delete_ignores_out_of_scope_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()

    class FakeNotesFetcher:
        def __init__(self, *, token_info, instance_url):  # noqa: ANN001
            pass

        async def fetch_one_by_link_id(self, link_id: str, *, linked_entity_ids):
            assert link_id == "06Axx0000000001AAA"
            assert linked_entity_ids == ["006xx0000000001AAA"]
            return []

        async def fetch_link_by_id(self, link_id: str):
            assert link_id == "06Axx0000000001AAA"
            return {
                "Id": link_id,
                "ContentDocumentId": "069xx0000000001AAA",
                "LinkedEntityId": "500xx0000000001AAA",
            }

    class FakeNoteService:
        async def _load_context(self, organisation_id, integration_id):  # noqa: ANN001
            return SimpleNamespace(token_info="token-info")

        async def _load_linked_entity_ids(self, organisation_id, integration_id):  # noqa: ANN001
            return ["006xx0000000001AAA"]

    async def fail_delete(**_kwargs):
        raise AssertionError("out-of-scope links must not delete local notes")

    monkeypatch.setattr(salesforce_cdc_subscriber_task, "SalesforceNotesFetcher", FakeNotesFetcher)
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "SalesforceNoteRecordSyncService",
        FakeNoteService,
    )
    monkeypatch.setattr(salesforce_cdc_subscriber_task, "_soft_delete_note", fail_delete)
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "_soft_delete_note_by_content_document_link_id",
        fail_delete,
    )

    await salesforce_cdc_subscriber_task._process_record_change(
        organisation_id=org_id,
        integration_id=integration_id,
        record_id="06Axx0000000001AAA",
        change_type="DELETE",
        instance_url="https://acme.my.salesforce.com",
        channel="/data/ContentDocumentLinkChangeEvent",
    )


@pytest.mark.asyncio
async def test_content_document_link_create_fetch_miss_does_not_soft_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()

    class FakeNotesFetcher:
        def __init__(self, *, token_info, instance_url):  # noqa: ANN001
            pass

        async def fetch_one_by_link_id(self, link_id: str, *, linked_entity_ids):
            assert link_id == "06Axx0000000001AAA"
            assert linked_entity_ids == ["006xx0000000001AAA"]
            return []

        async def fetch_link_by_id(self, link_id: str):
            raise AssertionError("create fetch misses should not run delete fallback")

    class FakeNoteService:
        async def _load_context(self, organisation_id, integration_id):  # noqa: ANN001
            return SimpleNamespace(token_info="token-info")

        async def _load_linked_entity_ids(self, organisation_id, integration_id):  # noqa: ANN001
            return ["006xx0000000001AAA"]

    async def fail_delete(**_kwargs):
        raise AssertionError("create fetch misses must not delete local notes")

    monkeypatch.setattr(salesforce_cdc_subscriber_task, "SalesforceNotesFetcher", FakeNotesFetcher)
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "SalesforceNoteRecordSyncService",
        FakeNoteService,
    )
    monkeypatch.setattr(salesforce_cdc_subscriber_task, "_soft_delete_note", fail_delete)
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "_soft_delete_note_by_content_document_link_id",
        fail_delete,
    )

    await salesforce_cdc_subscriber_task._process_record_change(
        organisation_id=org_id,
        integration_id=integration_id,
        record_id="06Axx0000000001AAA",
        change_type="CREATE",
        instance_url="https://acme.my.salesforce.com",
        channel="/data/ContentDocumentLinkChangeEvent",
    )


@pytest.mark.asyncio
async def test_process_task_record_change_fetches_and_persists_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    calls: list[dict[str, object]] = []

    class FakeTaskFetcher:
        def __init__(self, *, token_info, instance_url):  # noqa: ANN001
            calls.append({"fetcher_instance_url": instance_url, "token_info": token_info})

        async def fetch_one(self, task_id: str):
            calls.append({"fetch_one": task_id})
            return {"Id": task_id, "Subject": "Follow up"}

    class FakeTaskService:
        async def _load_context(self, organisation_id, integration_id):  # noqa: ANN001
            calls.append({"load_context": (organisation_id, integration_id)})
            return SimpleNamespace(token_info="token-info")

        async def persist_single_task(self, **kwargs):
            calls.append({"persist": kwargs})

    monkeypatch.setattr(salesforce_cdc_subscriber_task, "SalesforceTasksFetcher", FakeTaskFetcher)
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "SalesforceTaskRecordSyncService",
        FakeTaskService,
    )

    await salesforce_cdc_subscriber_task._process_record_change(
        organisation_id=org_id,
        integration_id=integration_id,
        record_id="00Txx0000000001AAA",
        change_type="UPDATE",
        instance_url="https://acme.my.salesforce.com",
        channel="/data/TaskChangeEvent",
    )

    assert calls[-1]["persist"]["raw_task"]["Id"] == "00Txx0000000001AAA"


@pytest.mark.asyncio
async def test_process_user_record_change_fetches_and_persists_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    calls: list[dict[str, object]] = []

    class FakeUserFetcher:
        def __init__(self, *, token_info, instance_url):  # noqa: ANN001
            calls.append({"fetcher_instance_url": instance_url, "token_info": token_info})

        async def fetch_one(self, user_id: str):
            calls.append({"fetch_one": user_id})
            return {"Id": user_id, "Name": "Alex Morgan", "IsActive": True}

    class FakeUserService:
        async def _load_context(self, organisation_id, integration_id):  # noqa: ANN001
            calls.append({"load_context": (organisation_id, integration_id)})
            return SimpleNamespace(token_info="token-info")

        async def persist_single_user(self, **kwargs):
            calls.append({"persist": kwargs})

    monkeypatch.setattr(salesforce_cdc_subscriber_task, "SalesforceUsersFetcher", FakeUserFetcher)
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "SalesforceUserRecordSyncService",
        FakeUserService,
    )

    await salesforce_cdc_subscriber_task._process_record_change(
        organisation_id=org_id,
        integration_id=integration_id,
        record_id="005xx0000000001AAA",
        change_type="UPDATE",
        instance_url="https://acme.my.salesforce.com",
        channel="/data/UserChangeEvent",
    )

    assert calls[-1]["persist"]["raw_user"]["Id"] == "005xx0000000001AAA"


@pytest.mark.asyncio
async def test_process_user_delete_deactivates_crm_user_without_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    deactivated: list[str] = []

    async def fake_deactivate_user(**kwargs):
        deactivated.append(kwargs["external_id"])

    def fail_fetcher(*_args, **_kwargs):
        raise AssertionError("delete should not fetch User")

    monkeypatch.setattr(salesforce_cdc_subscriber_task, "_deactivate_user", fake_deactivate_user)
    monkeypatch.setattr(salesforce_cdc_subscriber_task, "SalesforceUsersFetcher", fail_fetcher)

    await salesforce_cdc_subscriber_task._process_record_change(
        organisation_id=org_id,
        integration_id=integration_id,
        record_id="005xx0000000001AAA",
        change_type="DELETE",
        instance_url="https://acme.my.salesforce.com",
        channel="/data/UserChangeEvent",
    )

    assert deactivated == ["005xx0000000001AAA"]


@pytest.mark.asyncio
async def test_process_content_document_record_change_fetches_and_persists_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    calls: list[dict[str, object]] = []

    class FakeNotesFetcher:
        def __init__(self, *, token_info, instance_url):  # noqa: ANN001
            calls.append({"fetcher_instance_url": instance_url, "token_info": token_info})

        async def fetch_one_by_note_id(self, note_id: str, *, linked_entity_ids):
            calls.append({"fetch_one_by_note_id": note_id, "linked_entity_ids": linked_entity_ids})
            return [{"Id": note_id, "LinkedEntityId": "006xx0000000001AAA"}]

    class FakeNoteService:
        async def _load_context(self, organisation_id, integration_id):  # noqa: ANN001
            calls.append({"load_context": (organisation_id, integration_id)})
            return SimpleNamespace(token_info="token-info")

        async def _load_linked_entity_ids(self, organisation_id, integration_id):  # noqa: ANN001
            return ["006xx0000000001AAA"]

        async def persist_single_note(self, **kwargs):
            calls.append({"persist": kwargs})

    monkeypatch.setattr(salesforce_cdc_subscriber_task, "SalesforceNotesFetcher", FakeNotesFetcher)
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "SalesforceNoteRecordSyncService",
        FakeNoteService,
    )

    await salesforce_cdc_subscriber_task._process_record_change(
        organisation_id=org_id,
        integration_id=integration_id,
        record_id="069xx0000000001AAA",
        change_type="UPDATE",
        instance_url="https://acme.my.salesforce.com",
        channel="/data/ContentDocumentChangeEvent",
    )

    assert calls[-1]["persist"]["raw_notes"][0]["Id"] == "069xx0000000001AAA"


@pytest.mark.asyncio
async def test_replay_repair_routes_task_and_note_channels_to_matching_full_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    queued: list[dict[str, object]] = []

    async def fake_send(*args, **kwargs):
        queued.append({"task_name": args[0], **kwargs})

    monkeypatch.setattr(subscriber_task, "send_integrations_task_async", fake_send)

    for channel in (
        "/data/TaskChangeEvent",
        "/data/ContentDocumentChangeEvent",
        "/data/ContentDocumentLinkChangeEvent",
    ):
        await subscriber_task._enqueue_repair_sync(
            organisation_id=org_id,
            integration_id=integration_id,
            channel=channel,
        )

    assert [entry["task_name"] for entry in queued] == [
        "integrations.sync_salesforce_tasks",
        "integrations.sync_salesforce_notes",
        "integrations.sync_salesforce_notes",
    ]
    assert [entry["kwargs"]["cdc_channel"] for entry in queued] == [
        "/data/TaskChangeEvent",
        "/data/ContentDocumentChangeEvent",
        "/data/ContentDocumentLinkChangeEvent",
    ]


@pytest.mark.asyncio
async def test_replay_repair_routes_user_channel_to_user_full_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    queued: list[dict[str, object]] = []

    async def fake_send(*args, **kwargs):
        queued.append({"task_name": args[0], **kwargs})

    monkeypatch.setattr(subscriber_task, "send_integrations_task_async", fake_send)

    await subscriber_task._enqueue_repair_sync(
        organisation_id=org_id,
        integration_id=integration_id,
        channel="/data/UserChangeEvent",
    )

    assert queued[0]["task_name"] == "integrations.sync_salesforce_users"
    assert queued[0]["kwargs"]["cdc_channel"] == "/data/UserChangeEvent"


class _BaseStateService:
    async def acquire_lease(self, **_kwargs):
        return SimpleNamespace(acquired=True, subscription=SimpleNamespace(last_replay_id=None))

    async def mark_error(self, **_kwargs):
        return None

    async def release_lease(self, **_kwargs):
        return True


def _patch_context(
    monkeypatch: pytest.MonkeyPatch,
    org_id: UUID,
    integration_id: UUID,
) -> None:
    async def fake_load_integration(**_kwargs):
        return SimpleNamespace(
            organisation_id=org_id,
            integration_id=integration_id,
            credentials={"access_token": "token", "instance_url": "https://sf"},
            access_token_expires_at=None,
            integration_type="salesforce",
            scopes=[],
            connection_id="conn-1",
            connection_metadata={"salesforce_organization_id": "00Dxx0000000001"},
        )

    def fake_metadata(_integration):
        return SimpleNamespace(
            channel="/data/OpportunityChangeEvent",
            instance_url="https://sf",
            salesforce_organization_id="00Dxx0000000001",
            pubsub_endpoint="api.pubsub.salesforce.com:443",
        )

    monkeypatch.setattr(subscriber_task, "_load_integration", fake_load_integration)
    monkeypatch.setattr(
        subscriber_task,
        "get_salesforce_opportunity_cdc_metadata",
        fake_metadata,
    )
    monkeypatch.setattr(subscriber_task, "get_access_token", AsyncMock(return_value="token"))
    monkeypatch.setattr(subscriber_task, "acquire_org_async_session", _acquire_session)
