"""Tests for stakeholder research service internals."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import src.workers.deal_worker.services.deal_insight_services.stakeholder_research_service as service_module
from src.agents.workflows.stakeholder_research.models import StakeholderResearchResult
from src.domain.models.organisation_tables.sync.crm.deals import CRMParticipantType
from src.domain.models.organisation_tables.sync.crm.stakeholders import ApprovalStatus
from src.workers.deal_worker.services.deal_insight_services.stakeholder_research_service import (
    StakeholderResearchService,
)


class _Result:
    def __init__(self, row=None):
        self.row = row

    def scalar_one_or_none(self):
        return self.row


class _FakeSession:
    def __init__(self, contact: SimpleNamespace):
        self._contact = contact
        self.execute = AsyncMock()
        self.commit = AsyncMock()

    async def get(self, _model, _contact_id):  # noqa: ANN001 - model type is not relevant here
        return self._contact


class _ParticipantLookupSession:
    def __init__(self, participant: SimpleNamespace, contact: SimpleNamespace):
        self.participant = participant
        self.contact = contact
        self.execute_calls = 0

    async def get(self, model, item_id):  # noqa: ANN001 - model type is not relevant here
        return self.contact

    async def execute(self, stmt):  # noqa: ANN001 - statement shape is asserted by behavior
        self.execute_calls += 1
        return _Result(self.participant)


class _WriteCaptureSession:
    def __init__(self, contact: SimpleNamespace):
        self.contact = contact
        self.updates: list[tuple[str, dict[str, object]]] = []
        self.commits = 0

    async def get(self, model, item_id):  # noqa: ANN001 - model type is not relevant here
        return self.contact

    async def execute(self, stmt):  # noqa: ANN001 - SQLAlchemy update object
        table = getattr(getattr(stmt, "table", None), "name", "")
        self.updates.append((table, _update_values(stmt)))
        return _Result()

    async def commit(self):
        self.commits += 1


class _SessionSource:
    def __init__(self, session):
        self.session = session

    async def __aiter__(self):
        yield self.session


def _update_values(stmt) -> dict[str, object]:  # noqa: ANN001 - SQLAlchemy private shape
    values = {}
    for key, bind in getattr(stmt, "_values", {}).items():
        column_name = getattr(key, "key", str(key))
        values[column_name] = getattr(bind, "value", bind)
    return values


@pytest.mark.asyncio
async def test_load_context_reads_active_contact_participant(monkeypatch) -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    participant_id = uuid4()
    contact_id = uuid4()
    participant = SimpleNamespace(
        organisation_id=organisation_id,
        participant_id=participant_id,
        participant_type=CRMParticipantType.CONTACT,
        is_deleted=False,
        approval_status=ApprovalStatus.APPROVED.value,
        deal_id=deal_id,
        related_local_id=contact_id,
        related_email="buyer@example.com",
        related_name="Buyer Snapshot",
        job_title="Old Title",
        external_links=[],
    )
    contact = SimpleNamespace(
        organisation_id=organisation_id,
        contact_id=contact_id,
        is_deleted=False,
        email="buyer@example.com",
        full_name="Buyer Contact",
        job_title="CFO",
        properties={
            "department": "Finance",
            "external_links": [{"platform": "linkedin", "url": "https://linkedin/in/buyer"}],
        },
    )
    session = _ParticipantLookupSession(participant, contact)
    monkeypatch.setattr(
        service_module,
        "acquire_org_async_session",
        lambda organisation_id: _SessionSource(session),
    )

    ctx = await StakeholderResearchService(organisation_id)._load_context(  # noqa: SLF001
        participant_id,
        None,
    )

    assert ctx.email == "buyer@example.com"
    assert ctx.name == "Buyer Contact"
    assert ctx.deal_id == deal_id
    assert ctx.contact_id == contact_id
    assert ctx.external_links == [{"platform": "linkedin", "url": "https://linkedin/in/buyer"}]


@pytest.mark.asyncio
async def test_persist_result_writes_contact_and_participant_research(monkeypatch) -> None:
    organisation_id = uuid4()
    participant_id = uuid4()
    contact_id = uuid4()
    contact = SimpleNamespace(
        organisation_id=organisation_id,
        contact_id=contact_id,
        is_deleted=False,
        first_name=None,
        last_name=None,
        full_name=None,
        job_title=None,
        properties={},
    )
    session = _WriteCaptureSession(contact)
    monkeypatch.setattr(
        service_module,
        "acquire_org_async_session",
        lambda organisation_id: _SessionSource(session),
    )

    ctx = service_module._StakeholderContext(
        email="buyer@example.com",
        name=None,
        deal_id=uuid4(),
        contact_id=contact_id,
        external_links=[],
    )
    result = StakeholderResearchResult(
        status="found",
        name="Ada Lovelace",
        first_name="Ada",
        last_name="Lovelace",
        job_title="CTO",
        linkedin_url="https://linkedin/in/ada",
        research_markdown="## Ada\nResearch profile",
    )

    markdown = await StakeholderResearchService(organisation_id)._persist_result(  # noqa: SLF001
        participant_id,
        ctx,
        result,
    )

    assert markdown == "## Ada\nResearch profile"
    updates_by_table = dict(session.updates)
    assert "deal_stakeholders" not in updates_by_table
    assert updates_by_table["crm_contacts"]["contact_research"] == "## Ada\nResearch profile"
    assert isinstance(updates_by_table["crm_contacts"]["last_researched_at"], datetime)
    assert updates_by_table["crm_deal_participants"]["stakeholder_research"] == (
        "## Ada\nResearch profile"
    )
    assert updates_by_table["crm_deal_participants"]["related_name"] == "Ada Lovelace"
    assert updates_by_table["crm_deal_participants"]["job_title"] == "CTO"
    assert updates_by_table["crm_deal_participants"]["linkedin_url"] == ("https://linkedin/in/ada")
    assert updates_by_table["crm_contacts"]["linkedin_url"] == "https://linkedin/in/ada"
    assert updates_by_table["crm_deal_participants"]["department"] == "Engineering"
    assert session.commits >= 1


@pytest.mark.asyncio
async def test_fill_contact_identity_includes_stakeholder_id_in_log_context(monkeypatch) -> None:
    organisation_id = uuid4()
    stakeholder_id = uuid4()
    contact_id = uuid4()
    service = StakeholderResearchService(organisation_id)

    contact = SimpleNamespace(
        organisation_id=organisation_id,
        first_name=None,
        last_name=None,
        full_name=None,
        job_title=None,
    )
    session = _FakeSession(contact)

    async def fake_acquire_org_async_session(_organisation_id):  # noqa: ANN001
        yield session

    logger_info = MagicMock()
    monkeypatch.setattr(service_module, "acquire_org_async_session", fake_acquire_org_async_session)
    monkeypatch.setattr(service_module, "logger", SimpleNamespace(info=logger_info))

    result = StakeholderResearchResult(
        status="found",
        name="Ada Lovelace",
        first_name="Ada",
        last_name="Lovelace",
        job_title="CTO",
    )

    await service._fill_contact_identity(  # noqa: SLF001 - internal behavior regression test
        contact_id=contact_id,
        stakeholder_id=stakeholder_id,
        result=result,
    )

    session.execute.assert_awaited_once()
    session.commit.assert_awaited_once()
    assert logger_info.call_args.kwargs["extra"]["stakeholder_id"] == str(stakeholder_id)
