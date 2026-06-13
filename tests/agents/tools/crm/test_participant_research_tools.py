from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import import_module
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import CRMParticipantType

enrich_module = import_module("src.agents.tools.crm.enrich_stakeholder")
research_module = import_module("src.agents.tools.research.research_stakeholder")


class _Result:
    def __init__(self, row=None):
        self.row = row

    def scalar_one_or_none(self):
        return self.row


class _Session:
    def __init__(self, participant: SimpleNamespace, contact: SimpleNamespace):
        self.participant = participant
        self.contact = contact
        self.updates: list[tuple[str, dict[str, object]]] = []
        self.commits = 0

    async def get(self, model, item_id):  # noqa: ANN001 - model is not relevant for fake
        return self.contact

    async def execute(self, stmt):  # noqa: ANN001 - SQLAlchemy statement object
        table = getattr(getattr(stmt, "table", None), "name", None)
        if table:
            self.updates.append((table, _update_values(stmt)))
            return _Result()
        return _Result(self.participant)

    async def commit(self):
        self.commits += 1


@dataclass
class _Context:
    organisation_id: UUID
    deal_id: UUID
    session: _Session

    @asynccontextmanager
    async def acquire_session(self):
        yield self.session


def _update_values(stmt) -> dict[str, object]:  # noqa: ANN001 - SQLAlchemy private shape
    values = {}
    for key, bind in getattr(stmt, "_values", {}).items():
        column_name = getattr(key, "key", str(key))
        values[column_name] = getattr(bind, "value", bind)
    return values


def _make_context() -> tuple[_Context, _Session]:
    organisation_id = uuid4()
    deal_id = uuid4()
    participant_id = uuid4()
    contact_id = uuid4()
    participant = SimpleNamespace(
        organisation_id=organisation_id,
        participant_id=participant_id,
        deal_id=deal_id,
        participant_type=CRMParticipantType.CONTACT,
        is_deleted=False,
        related_local_id=contact_id,
        related_email="buyer@example.com",
        related_name=None,
        job_title=None,
        department=None,
        external_links=[],
    )
    contact = SimpleNamespace(
        organisation_id=organisation_id,
        contact_id=contact_id,
        is_deleted=False,
        email="buyer@example.com",
        full_name=None,
        job_title=None,
        properties={},
    )
    session = _Session(participant, contact)
    return _Context(organisation_id=organisation_id, deal_id=deal_id, session=session), session


@pytest.mark.asyncio
async def test_research_stakeholder_reads_participant_context(monkeypatch) -> None:
    context, session = _make_context()

    async def fake_apollo_enrich_person(**kwargs):  # noqa: ANN003
        return {
            "success": True,
            "apollo_id": "person-1",
            "name": "Buyer Contact",
            "first_name": "Buyer",
            "last_name": "Contact",
            "title": "CFO",
            "linkedin_url": "https://linkedin/in/buyer",
        }

    monkeypatch.setattr(research_module, "apollo_enrich_person", fake_apollo_enrich_person)

    result = await research_module.research_stakeholder(
        context=context,  # type: ignore[arg-type]
        stakeholder_id=str(session.participant.participant_id),
    )

    assert result["success"] is True
    assert result["participant_id"] == str(session.participant.participant_id)
    assert "stakeholder_id" not in result
    assert result["data"]["title"] == "CFO"
    assert session.updates == []


@pytest.mark.asyncio
async def test_enrich_stakeholder_writes_contact_and_participant(monkeypatch) -> None:
    context, session = _make_context()

    async def fake_apollo_enrich_person(**kwargs):  # noqa: ANN003
        return {
            "success": True,
            "name": "Ada Lovelace",
            "title": "CTO",
            "linkedin_url": "https://linkedin/in/ada",
        }

    monkeypatch.setattr(enrich_module, "apollo_enrich_person", fake_apollo_enrich_person)

    result = await enrich_module.enrich_stakeholder(
        context=context,  # type: ignore[arg-type]
        stakeholder_id=str(session.participant.participant_id),
    )

    updates_by_table = dict(session.updates)
    assert result["success"] is True
    assert result["participant_id"] == str(session.participant.participant_id)
    assert "deal_stakeholders" not in updates_by_table
    assert updates_by_table["crm_deal_participants"]["related_name"] == "Ada Lovelace"
    assert updates_by_table["crm_deal_participants"]["job_title"] == "CTO"
    assert updates_by_table["crm_contacts"]["full_name"] == "Ada Lovelace"
    assert updates_by_table["crm_contacts"]["job_title"] == "CTO"
    assert session.commits == 1
