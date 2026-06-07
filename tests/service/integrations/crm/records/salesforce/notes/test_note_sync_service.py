from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.crm.source_mappings import CRMSourceObjectType
from src.service.integrations.crm.records.salesforce.notes import note_sync_service
from src.service.integrations.crm.records.salesforce.notes.note_sync_service import (
    SalesforceNoteRecordSyncService,
    SalesforceNoteSyncContext,
    _load_live_external_ids,
)
from src.service.integrations.crm.records.salesforce.notes.note_types import (
    NormalizedSalesforceNote,
    NoteWriteResult,
)
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo


def _normalized_note(
    external_id: str = "069xx0000000001AAA",
    *,
    owner_external_id: str | None = None,
    deal_external_id: str | None = "006xx0000000001AAA",
    contact_external_id: str | None = None,
    company_external_id: str | None = None,
) -> NormalizedSalesforceNote:
    return NormalizedSalesforceNote(
        external_id=external_id,
        subject="QBR summary",
        body="Talked about renewal.",
        activity_subtype="SALESFORCE_NOTE",
        owner_external_id=owner_external_id,
        deal_external_id=deal_external_id,
        contact_external_id=contact_external_id,
        company_external_id=company_external_id,
        related_object_type="DEAL",
        related_external_id="006xx0000000001AAA",
        source_created_at=None,
        source_modified_at=None,
        properties={},
        data_payload={},
    )


@pytest.mark.asyncio
async def test_run_buffers_notes_before_persisting_and_then_deactivates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    first_note = _normalized_note(external_id="069A", deal_external_id="006A")
    second_note = _normalized_note(external_id="069B", deal_external_id="006B")
    persist_calls: list[dict[str, object]] = []

    class FakeFetcher:
        def __init__(self, **_kwargs) -> None:
            pass

        async def iter_batches(self, *, linked_entity_ids):
            assert linked_entity_ids == ["006A", "006B"]
            yield [{"Id": "069A"}]
            yield [{"Id": "069B"}]

    async def fake_load_context(self, organisation_id, integration_id):
        return SalesforceNoteSyncContext(
            token_info=IntegrationTokenInfo(
                access_token="token",
                refresh_token="refresh",
                integration_type="salesforce",
                organisation_id=organisation_id,
                integration_id=integration_id,
            ),
            instance_url="https://acme.my.salesforce.com",
        )

    async def fake_load_initial_sync_linked_entity_ids(self, organisation_id, integration_id):
        return ["006A", "006B"]

    def fake_normalize_notes(raw_notes):
        mapping = {"069A": first_note, "069B": second_note}
        return [mapping[row["Id"]] for row in raw_notes], 0

    async def fake_persist(self, **kwargs):
        persist_calls.append(kwargs)
        return NoteWriteResult(notes_created=len(kwargs["normalized"]))

    monkeypatch.setattr(note_sync_service, "SalesforceNotesFetcher", FakeFetcher)
    monkeypatch.setattr(SalesforceNoteRecordSyncService, "_load_context", fake_load_context)
    monkeypatch.setattr(
        SalesforceNoteRecordSyncService,
        "_load_initial_sync_linked_entity_ids",
        fake_load_initial_sync_linked_entity_ids,
    )
    monkeypatch.setattr(note_sync_service, "normalize_notes", fake_normalize_notes)
    monkeypatch.setattr(SalesforceNoteRecordSyncService, "_persist", fake_persist)

    result = await SalesforceNoteRecordSyncService().run(
        organisation_id=org_id,
        integration_id=integration_id,
    )

    assert result.fetched_notes == 2
    assert result.notes_created == 2
    assert len(persist_calls) == 2
    assert persist_calls[0]["normalized"] == [first_note, second_note]
    assert persist_calls[0]["deactivate_missing"] is False
    assert persist_calls[1]["normalized"] == []
    assert persist_calls[1]["deactivate_missing"] is True
    assert persist_calls[1]["deactivate_deal_external_ids"] == ["006A", "006B"]
    assert "all_external_ids" not in persist_calls[1]
    assert persist_calls[0]["snapshot_started_at"] == persist_calls[1]["snapshot_started_at"]


@pytest.mark.asyncio
async def test_persist_single_note_writes_only_the_changed_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    note = _normalized_note()
    persist_calls: list[dict[str, object]] = []

    def fake_normalize_notes(raw_notes):
        assert raw_notes == [{"Id": note.external_id}]
        return [note], 0

    async def fake_persist(self, **kwargs):
        persist_calls.append(kwargs)
        return NoteWriteResult(notes_reactivated=1)

    monkeypatch.setattr(note_sync_service, "normalize_notes", fake_normalize_notes)
    monkeypatch.setattr(SalesforceNoteRecordSyncService, "_persist", fake_persist)

    result = await SalesforceNoteRecordSyncService().persist_single_note(
        organisation_id=org_id,
        integration_id=integration_id,
        raw_notes=[{"Id": note.external_id}],
    )

    assert result.notes_reactivated == 1
    assert persist_calls[0]["normalized"] == [note]
    assert persist_calls[0]["deactivate_missing"] is False
    assert persist_calls[0].get("deactivate_deal_external_ids") is None


@pytest.mark.asyncio
async def test_persist_loads_activity_maps_for_current_notes_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SalesforceNoteRecordSyncService()
    captured: dict[str, set[str]] = {}

    class FakeSession:
        async def commit(self) -> None:
            pass

    async def fake_acquire_session(_organisation_id):
        yield FakeSession()

    class FakeWriter:
        def __init__(self, **_kwargs) -> None:
            pass

        async def apply(self, **_kwargs):
            return NoteWriteResult(notes_created=1)

    async def fake_load_user_map(session, organisation_id, integration_id, external_ids):
        captured["user_external_ids"] = set(external_ids)
        return {}

    async def fake_load_deal_map(session, organisation_id, integration_id, external_ids):
        captured["deal_external_ids"] = set(external_ids)
        return {}

    async def fake_load_contact_map(session, organisation_id, integration_id, external_ids):
        captured["contact_external_ids"] = set(external_ids)
        return {}

    async def fake_load_company_map(session, organisation_id, integration_id, external_ids):
        captured["company_external_ids"] = set(external_ids)
        return {}

    monkeypatch.setattr(note_sync_service, "acquire_org_async_session", fake_acquire_session)
    monkeypatch.setattr(note_sync_service, "SalesforceNoteWriter", FakeWriter)
    monkeypatch.setattr(service, "_load_user_map", fake_load_user_map)
    monkeypatch.setattr(service, "_load_deal_map", fake_load_deal_map)
    monkeypatch.setattr(service, "_load_contact_map", fake_load_contact_map)
    monkeypatch.setattr(service, "_load_company_map", fake_load_company_map)

    result = await service._persist(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        normalized=[
            _normalized_note(
                owner_external_id="005A",
                deal_external_id="006A",
                contact_external_id="003A",
                company_external_id="001A",
            )
        ],
        snapshot_started_at=None,
    )

    assert result.notes_created == 1
    assert captured == {
        "user_external_ids": {"005A"},
        "deal_external_ids": {"006A"},
        "contact_external_ids": {"003A"},
        "company_external_ids": {"001A"},
    }


@pytest.mark.asyncio
async def test_dimension_maps_resolve_through_source_mappings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SalesforceNoteRecordSyncService()
    organisation_id = uuid4()
    integration_id = uuid4()
    user_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    company_id = uuid4()
    mapping_calls: list[tuple[CRMSourceObjectType, set[str] | None]] = []

    class FakeMappingHelper:
        def __init__(self, **_kwargs) -> None:
            pass

        async def load_local_id_map(self, *, object_type, external_ids=None, **_kwargs):
            mapping_calls.append((object_type, set(external_ids) if external_ids else None))
            return {
                CRMSourceObjectType.USER: {"005A": user_id},
                CRMSourceObjectType.DEAL: {"006A": deal_id},
                CRMSourceObjectType.CONTACT: {"003A": contact_id},
                CRMSourceObjectType.COMPANY: {"001A": company_id},
            }[object_type]

    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(all=lambda: [(user_id, "Owner", "owner@example.com")]),
            SimpleNamespace(all=lambda: [(deal_id, "Big Deal")]),
            SimpleNamespace(all=lambda: [(contact_id, "Alex Morgan", "alex@example.com")]),
            SimpleNamespace(all=lambda: [(company_id, "Acme")]),
        ]
    )
    monkeypatch.setattr(note_sync_service, "CRMSourceMappingHelper", FakeMappingHelper)

    user_map = await service._load_user_map(session, organisation_id, integration_id, {"005A"})
    deal_map = await service._load_deal_map(session, organisation_id, integration_id, {"006A"})
    contact_map = await service._load_contact_map(
        session, organisation_id, integration_id, {"003A"}
    )
    company_map = await service._load_company_map(
        session, organisation_id, integration_id, {"001A"}
    )

    assert user_map["005A"].email == "owner@example.com"
    assert deal_map["006A"].name == "Big Deal"
    assert contact_map["003A"].name == "Alex Morgan"
    assert company_map["001A"].internal_id == company_id
    assert mapping_calls == [
        (CRMSourceObjectType.USER, {"005A"}),
        (CRMSourceObjectType.DEAL, {"006A"}),
        (CRMSourceObjectType.CONTACT, {"003A"}),
        (CRMSourceObjectType.COMPANY, {"001A"}),
    ]


@pytest.mark.asyncio
async def test_load_linked_entity_ids_keeps_cdc_note_scope_broad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SalesforceNoteRecordSyncService()
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    company_id = uuid4()
    mapping_calls: list[tuple[CRMSourceObjectType, set[str] | None]] = []

    class FakeMappingHelper:
        def __init__(self, **_kwargs) -> None:
            pass

        async def load_local_id_map(self, *, object_type, external_ids=None, **_kwargs):
            mapping_calls.append((object_type, set(external_ids) if external_ids else None))
            return {
                CRMSourceObjectType.DEAL: {"006A": deal_id, "006STALE": uuid4()},
                CRMSourceObjectType.CONTACT: {"003A": contact_id},
                CRMSourceObjectType.COMPANY: {"001A": company_id},
            }[object_type]

    class FakeSession:
        execute = AsyncMock(
            side_effect=[
                SimpleNamespace(all=lambda: [(deal_id,)]),
                SimpleNamespace(all=lambda: [(contact_id,)]),
                SimpleNamespace(all=lambda: [(company_id,)]),
            ]
        )

    async def fake_acquire_session(_organisation_id):
        yield FakeSession()

    monkeypatch.setattr(note_sync_service, "CRMSourceMappingHelper", FakeMappingHelper)
    monkeypatch.setattr(note_sync_service, "acquire_org_async_session", fake_acquire_session)

    linked_entity_ids = await service._load_linked_entity_ids(organisation_id, integration_id)

    assert linked_entity_ids == ["006A", "003A", "001A"]
    assert mapping_calls == [
        (CRMSourceObjectType.DEAL, None),
        (CRMSourceObjectType.CONTACT, None),
        (CRMSourceObjectType.COMPANY, None),
    ]


@pytest.mark.asyncio
async def test_load_initial_sync_linked_entity_ids_scopes_notes_to_eligible_deals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SalesforceNoteRecordSyncService()
    organisation_id = uuid4()
    integration_id = uuid4()

    async def fake_load_initial_sync_deal_external_ids(**kwargs):
        assert kwargs["organisation_id"] == organisation_id
        assert kwargs["integration_id"] == integration_id
        return ["006OPEN", "006RECENT"]

    monkeypatch.setattr(
        note_sync_service,
        "load_initial_sync_deal_external_ids",
        fake_load_initial_sync_deal_external_ids,
    )

    linked_entity_ids = await service._load_initial_sync_linked_entity_ids(
        organisation_id,
        integration_id,
    )

    assert linked_entity_ids == ["006OPEN", "006RECENT"]


@pytest.mark.asyncio
async def test_load_live_external_ids_chunks_local_id_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    local_ids = [uuid4(), uuid4(), uuid4()]

    async def fake_load_mapping_ids(**_kwargs):
        return {f"006{index}": local_id for index, local_id in enumerate(local_ids)}

    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(all=lambda: [(local_ids[0],), (local_ids[1],)]),
            SimpleNamespace(all=lambda: [(local_ids[2],)]),
        ]
    )
    monkeypatch.setattr(note_sync_service, "_load_mapping_ids", fake_load_mapping_ids)
    monkeypatch.setattr(note_sync_service, "LOCAL_ID_QUERY_CHUNK_SIZE", 2)

    external_ids = await _load_live_external_ids(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        provider="salesforce",
        object_type=CRMSourceObjectType.DEAL,
        model=CRMDeal,
        id_column=CRMDeal.deal_id,
    )

    assert external_ids == ["0060", "0061", "0062"]
    assert session.execute.await_count == 2
