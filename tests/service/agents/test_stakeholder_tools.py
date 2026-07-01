"""Unit tests for the upsert stakeholder agent tool."""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import src.agents.tools.crm.upsert_stakeholder as upsert_module
from src.agents.tools.crm.upsert_stakeholder import (
    UpsertStakeholderInput,
    create_upsert_stakeholder,
    upsert_stakeholder,
)
from src.agents.tools.registry import AGENT_TOOLS, ToolName
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    DecisionPower,
    StakeholderStance,
    StakeholderTemperature,
)


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.refreshed: list[object] = []

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj: object) -> None:
        self.refreshed.append(obj)

    async def rollback(self) -> None:
        self.rollbacks += 1


@dataclass
class _FakeContext:
    organisation_id: UUID
    deal_id: UUID
    session: _FakeSession
    user_id: UUID = field(default_factory=uuid4)
    metadata: dict = field(default_factory=dict)

    @asynccontextmanager
    async def acquire_session(self):
        yield self.session


def _participant(**overrides):
    participant_id = overrides.pop("participant_id", uuid4())
    values = {
        "participant_id": participant_id,
        "stakeholder_id": participant_id,
        "contact_id": None,
        "related_local_id": None,
        "related_email": "buyer@example.com",
        "email": "buyer@example.com",
        "linkedin_url": None,
        "related_name": "Buyer",
        "name": "Buyer",
        "job_title": None,
        "phone_number": None,
        "department": None,
        "stance": "UNKNOWN",
        "decision_power": "UNKNOWN",
        "temperature": "UNKNOWN",
        "motivation": None,
        "pain_points": None,
        "approval_status": "APPROVED",
        "is_deleted": False,
        "participant_source": "MANUAL",
        "stakeholder_research": None,
        "last_researched_at": None,
        "updated_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeStakeholdersService:
    def __init__(self, existing=None, created=None) -> None:
        self.existing = existing
        self.created = created or _participant()
        self.find_calls = []
        self.create_calls = []

    async def _is_internal_email(self, _email: str) -> bool:
        return False

    async def _find_contact_participant(self, **kwargs):
        self.find_calls.append(kwargs)
        return self.existing

    async def _get_participant_contact(self, _stakeholder):  # noqa: ANN001
        return None

    def _apply_contact_profile_updates(self, **_kwargs):  # noqa: ANN001
        return None

    async def create_stakeholder(self, deal_id, request, **_kwargs):  # noqa: ANN001
        self.create_calls.append((deal_id, request))
        return self.created


def _make_context() -> tuple[_FakeContext, _FakeSession]:
    session = _FakeSession()
    context = _FakeContext(
        organisation_id=uuid4(),
        deal_id=uuid4(),
        session=session,
        metadata={
            "_internal_guard_loaded": True,
            "_internal_domains": set(),
            "_internal_emails": set(),
        },
    )
    return context, session


@pytest.mark.asyncio
async def test_upsert_stakeholder_updates_existing_participant(monkeypatch) -> None:
    context, session = _make_context()
    existing = _participant(related_name=None, name=None, decision_power="UNKNOWN")
    service = _FakeStakeholdersService(existing=existing)
    monkeypatch.setattr(upsert_module, "StakeholderWriteService", lambda *_args, **_kwargs: service)

    result = await upsert_stakeholder(
        context=context,  # type: ignore[arg-type]
        input_data=UpsertStakeholderInput(
            email=" Buyer@Example.com ",
            name="Buyer Name",
            decision_power="DECISION_MAKER",
        ),
    )

    assert result is not None
    assert result["action"] == "updated"
    assert result["participant_id"] == str(existing.participant_id)
    assert "stakeholder_id" not in result
    assert existing.related_name is None
    assert existing.decision_power == DecisionPower.DECISION_MAKER.value
    assert session.commits == 1
    assert session.refreshed == [existing]


@pytest.mark.asyncio
async def test_upsert_only_fill_missing_does_not_overwrite_known_values(monkeypatch) -> None:
    context, session = _make_context()
    existing = _participant(related_name="Known Name", name="Known Name")
    service = _FakeStakeholdersService(existing=existing)
    monkeypatch.setattr(upsert_module, "StakeholderWriteService", lambda *_args, **_kwargs: service)

    result = await upsert_stakeholder(
        context=context,  # type: ignore[arg-type]
        input_data=UpsertStakeholderInput(
            email="known@example.com",
            name="New Name",
            only_fill_missing=True,
        ),
    )

    assert result is not None
    assert result["action"] == "no_change"
    assert existing.related_name == "Known Name"
    assert session.commits == 0


@pytest.mark.asyncio
async def test_upsert_stakeholder_creates_when_missing(monkeypatch) -> None:
    context, _session = _make_context()
    created = _participant(
        related_email="created@example.com",
        email="created@example.com",
        related_name="Created Buyer",
        name="Created Buyer",
    )
    service = _FakeStakeholdersService(existing=None, created=created)
    monkeypatch.setattr(upsert_module, "StakeholderWriteService", lambda *_args, **_kwargs: service)

    result = await upsert_stakeholder(
        context=context,  # type: ignore[arg-type]
        input_data=UpsertStakeholderInput(
            email="created@example.com",
            name="Created Buyer",
            decision_power="DECISION_MAKER",
        ),
    )

    assert result is not None
    assert result["action"] == "created"
    assert result["participant_id"] == str(created.participant_id)
    assert "stakeholder_id" not in result
    assert service.create_calls[0][0] == context.deal_id


@pytest.mark.asyncio
async def test_upsert_stakeholder_creates_linkedin_only_without_research(
    monkeypatch,
) -> None:
    context, _session = _make_context()
    created = _participant(
        related_email=None,
        email=None,
        linkedin_url="https://linkedin.com/in/created-buyer",
        related_name="Created Buyer",
        name="Created Buyer",
    )
    service = _FakeStakeholdersService(existing=None, created=created)
    monkeypatch.setattr(upsert_module, "StakeholderWriteService", lambda *_args, **_kwargs: service)

    result = await upsert_stakeholder(
        context=context,  # type: ignore[arg-type]
        input_data=UpsertStakeholderInput(
            linkedin_url="https://linkedin.com/in/created-buyer",
            name="Created Buyer",
        ),
    )

    assert result is not None
    assert result["action"] == "created"
    assert service.create_calls[0][1].linkedin_url == "https://linkedin.com/in/created-buyer"


@pytest.mark.asyncio
async def test_upsert_stakeholder_creates_name_only_placeholder(monkeypatch) -> None:
    context, _session = _make_context()
    created = _participant(
        related_email=None,
        email=None,
        related_name="Jane Buyer",
        name="Jane Buyer",
    )
    service = _FakeStakeholdersService(existing=None, created=created)
    monkeypatch.setattr(upsert_module, "StakeholderWriteService", lambda *_args, **_kwargs: service)

    result = await upsert_stakeholder(
        context=context,  # type: ignore[arg-type]
        input_data=UpsertStakeholderInput(identity="Jane Buyer"),
    )

    assert result is not None
    assert result["action"] == "created"
    assert result["email"] is None
    assert result["name"] == "Jane Buyer"
    assert service.create_calls[0][1].name == "Jane Buyer"


@pytest.mark.asyncio
async def test_upsert_stakeholder_tool_factory_handles_malformed_enum_payloads_on_update(
    monkeypatch,
) -> None:
    context, _session = _make_context()
    existing = _participant(stance="SUPPORTER", decision_power="INFLUENCER", temperature="WARM")
    service = _FakeStakeholdersService(existing=existing)
    monkeypatch.setattr(upsert_module, "StakeholderWriteService", lambda *_args, **_kwargs: service)
    tool = create_upsert_stakeholder(context, lambda: None)  # type: ignore[arg-type]

    result = await tool(
        email="malformed-update@acme.com",
        name="Normalized Update",
        stance={},  # type: ignore[arg-type]
        decision_power={},  # type: ignore[arg-type]
        temperature={},  # type: ignore[arg-type]
        only_fill_missing=False,
    )

    assert result is not None
    assert result["action"] == "no_change"
    assert "name" not in result["updated_fields"]
    assert "stance" not in result["updated_fields"]
    assert "decision_power" not in result["updated_fields"]
    assert "temperature" not in result["updated_fields"]
    assert existing.stance == StakeholderStance.SUPPORTER.value
    assert existing.decision_power == DecisionPower.INFLUENCER.value
    assert existing.temperature == StakeholderTemperature.WARM.value


@pytest.mark.asyncio
async def test_upsert_stakeholder_tool_factory_honors_create_if_missing_false(
    monkeypatch,
) -> None:
    context, _session = _make_context()
    service = _FakeStakeholdersService(existing=None)
    monkeypatch.setattr(upsert_module, "StakeholderWriteService", lambda *_args, **_kwargs: service)
    tool = create_upsert_stakeholder(context, lambda: None)  # type: ignore[arg-type]

    result = await tool(email="upsert-not-found@acme.com", create_if_missing=False)

    assert result is not None
    assert result["action"] == "not_found"
    assert result["updated"] is False
    assert service.create_calls == []


def test_upsert_stakeholder_tool_signature_does_not_expose_object_schema() -> None:
    context, _session = _make_context()
    tool = create_upsert_stakeholder(context, lambda: None)  # type: ignore[arg-type]

    params = inspect.signature(tool).parameters
    for field_name in ("stance", "decision_power", "temperature"):
        annotation = str(params[field_name].annotation)
        assert "dict" not in annotation


@pytest.mark.asyncio
async def test_upsert_stakeholder_rejects_internal_domain_email() -> None:
    context, _session = _make_context()
    context.metadata["_internal_domains"] = {"acme.com"}

    result = await upsert_stakeholder(
        context=context,  # type: ignore[arg-type]
        input_data=UpsertStakeholderInput(email="seller@acme.com", name="Seller"),
    )

    assert result is not None
    assert result["action"] == "rejected_internal_user"
    assert result["created"] is False
    assert result["updated"] is False


def test_registry_excludes_upsert_from_readonly_agents() -> None:
    readonly_agent_names = (
        "BriefingAgent",
        "DealSummaryAgent",
        "RiskBlockerExtractionAgent",
        "StakeholderAnalysisAgent",
    )
    for agent_name in readonly_agent_names:
        tools = AGENT_TOOLS[agent_name]
        assert ToolName.UPSERT_STAKEHOLDER not in tools, (
            f"{agent_name} should not have UPSERT_STAKEHOLDER"
        )


def test_registry_keeps_summary_agent_on_create_if_missing() -> None:
    tools = AGENT_TOOLS["SummaryAgent"]
    assert ToolName.CREATE_STAKEHOLDER_IF_MISSING in tools, (
        "SummaryAgent should create net-new external stakeholders discovered in meetings"
    )
    assert ToolName.UPSERT_STAKEHOLDER not in tools


def test_stakeholder_read_tools_use_read_service() -> None:
    for path in [
        Path("src/agents/tools/crm/list_stakeholders.py"),
        Path("src/agents/tools/crm/get_stakeholder_details.py"),
    ]:
        source = path.read_text()
        assert "select(CRMDealParticipant)" not in source
        assert "StakeholderReadService(" in source
