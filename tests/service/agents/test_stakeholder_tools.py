"""Unit tests for the upsert stakeholder agent tool."""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from src.agents.tools.crm.upsert_stakeholder import (
    UpsertStakeholderInput,
    create_upsert_stakeholder,
    upsert_stakeholder,
)
from src.agents.tools.registry import AGENT_TOOLS, ToolName
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    DealStakeholder,
    DecisionPower,
    StakeholderStance,
    StakeholderTemperature,
)


class _FakeScalarResult:
    def __init__(self, obj: DealStakeholder | None):
        self._obj = obj

    def scalar_one_or_none(self) -> DealStakeholder | None:
        return self._obj


class _FakeSession:
    def __init__(self, records: dict[str, DealStakeholder]):
        self.records = records
        self.pending: DealStakeholder | None = None

    async def execute(self, stmt):
        params = stmt.compile().params
        email_param = next((v for k, v in params.items() if k.startswith("email_")), None)
        email = str(email_param).lower() if email_param else ""
        return _FakeScalarResult(self.records.get(email))

    def add(self, obj: DealStakeholder) -> None:
        self.pending = obj

    async def commit(self) -> None:
        if self.pending is not None:
            self.records[self.pending.email.lower()] = self.pending
            self.pending = None

    async def refresh(self, _obj: DealStakeholder) -> None:
        return None

    async def rollback(self) -> None:
        return None


@dataclass
class _FakeContext:
    organisation_id: UUID
    deal_id: UUID
    session: _FakeSession

    @asynccontextmanager
    async def acquire_session(self):
        yield self.session


def _make_context(
    records: dict[str, DealStakeholder] | None = None,
) -> tuple[_FakeContext, dict[str, DealStakeholder]]:
    org_id = uuid4()
    deal_id = uuid4()
    store = records if records is not None else {}
    session = _FakeSession(store)
    context = _FakeContext(organisation_id=org_id, deal_id=deal_id, session=session)
    return context, store


@pytest.mark.asyncio
async def test_upsert_stakeholder_creates_new_record() -> None:
    context, store = _make_context()

    result = await upsert_stakeholder(
        context=context,  # type: ignore[arg-type]
        input_data=UpsertStakeholderInput(
            email=" Alice@Acme.com ",
            name="Alice Smith",
            decision_power="DECISION_MAKER",
        ),
    )

    assert result is not None
    assert result["action"] == "created"
    assert result["created"] is True
    assert result["email"] == "alice@acme.com"
    assert store["alice@acme.com"].name == "Alice Smith"
    assert store["alice@acme.com"].decision_power == DecisionPower.DECISION_MAKER


@pytest.mark.asyncio
async def test_upsert_stakeholder_fills_missing_fields_by_default() -> None:
    context, store = _make_context()

    await upsert_stakeholder(
        context=context,  # type: ignore[arg-type]
        input_data=UpsertStakeholderInput(email="cfo@acme.com"),
    )

    result = await upsert_stakeholder(
        context=context,  # type: ignore[arg-type]
        input_data=UpsertStakeholderInput(
            email="cfo@acme.com",
            name="Pat Lee",
            job_title="CFO",
            stance="CHAMPION",
            decision_power="DECISION_MAKER",
        ),
    )

    assert result is not None
    assert result["action"] == "updated"
    assert "name" in result["updated_fields"]
    assert "decision_power" in result["updated_fields"]
    assert store["cfo@acme.com"].name == "Pat Lee"
    assert store["cfo@acme.com"].job_title == "CFO"
    assert store["cfo@acme.com"].stance == StakeholderStance.CHAMPION
    assert store["cfo@acme.com"].decision_power == DecisionPower.DECISION_MAKER


@pytest.mark.asyncio
async def test_upsert_only_fill_missing_does_not_overwrite_known_values() -> None:
    context, store = _make_context()

    await upsert_stakeholder(
        context=context,  # type: ignore[arg-type]
        input_data=UpsertStakeholderInput(
            email="known@acme.com",
            name="Known Name",
            decision_power="INFLUENCER",
        ),
    )

    result = await upsert_stakeholder(
        context=context,  # type: ignore[arg-type]
        input_data=UpsertStakeholderInput(
            email="known@acme.com",
            name="New Name",
            decision_power="DECISION_MAKER",
            only_fill_missing=True,
        ),
    )

    assert result is not None
    assert result["action"] == "no_change"
    assert result["updated"] is False
    assert store["known@acme.com"].name == "Known Name"
    assert store["known@acme.com"].decision_power == DecisionPower.INFLUENCER


@pytest.mark.asyncio
async def test_upsert_can_overwrite_when_enabled() -> None:
    context, store = _make_context()

    await upsert_stakeholder(
        context=context,  # type: ignore[arg-type]
        input_data=UpsertStakeholderInput(
            email="overwrite@acme.com",
            name="Original Name",
            decision_power="INFLUENCER",
        ),
    )

    result = await upsert_stakeholder(
        context=context,  # type: ignore[arg-type]
        input_data=UpsertStakeholderInput(
            email="overwrite@acme.com",
            name="Updated Name",
            decision_power="DECISION_MAKER",
            only_fill_missing=False,
        ),
    )

    assert result is not None
    assert result["action"] == "updated"
    assert store["overwrite@acme.com"].name == "Updated Name"
    assert store["overwrite@acme.com"].decision_power == DecisionPower.DECISION_MAKER


@pytest.mark.asyncio
async def test_upsert_stakeholder_tool_factory_handles_malformed_enum_payloads_on_create() -> None:
    context, store = _make_context()
    tool = create_upsert_stakeholder(context, lambda: None)  # type: ignore[arg-type]

    result = await tool(
        email="malformed@acme.com",
        stance={},  # type: ignore[arg-type]
        decision_power={},  # type: ignore[arg-type]
        temperature={},  # type: ignore[arg-type]
    )

    assert result is not None
    assert result["action"] == "created"
    assert store["malformed@acme.com"].stance == StakeholderStance.UNKNOWN
    assert store["malformed@acme.com"].decision_power == DecisionPower.UNKNOWN
    assert store["malformed@acme.com"].temperature == StakeholderTemperature.UNKNOWN


@pytest.mark.asyncio
async def test_upsert_stakeholder_tool_factory_ignores_malformed_enum_payloads_on_update() -> None:
    context, store = _make_context()
    tool = create_upsert_stakeholder(context, lambda: None)  # type: ignore[arg-type]

    await tool(
        email="malformed-update@acme.com",
        stance="SUPPORTER",
        decision_power="INFLUENCER",
        temperature="WARM",
    )

    result = await tool(
        email="malformed-update@acme.com",
        name="Normalized Update",
        stance={},  # type: ignore[arg-type]
        decision_power={},  # type: ignore[arg-type]
        temperature={},  # type: ignore[arg-type]
        only_fill_missing=False,
    )

    assert result is not None
    assert result["action"] == "updated"
    assert "name" in result["updated_fields"]
    assert "stance" not in result["updated_fields"]
    assert "decision_power" not in result["updated_fields"]
    assert "temperature" not in result["updated_fields"]
    assert store["malformed-update@acme.com"].stance == StakeholderStance.SUPPORTER
    assert store["malformed-update@acme.com"].decision_power == DecisionPower.INFLUENCER
    assert store["malformed-update@acme.com"].temperature == StakeholderTemperature.WARM


@pytest.mark.asyncio
async def test_upsert_stakeholder_tool_factory_honors_create_if_missing_false() -> None:
    context, _ = _make_context()
    tool = create_upsert_stakeholder(context, lambda: None)  # type: ignore[arg-type]

    result = await tool(email="upsert-not-found@acme.com", create_if_missing=False)

    assert result is not None
    assert result["action"] == "not_found"
    assert result["updated"] is False


def test_upsert_stakeholder_tool_signature_does_not_expose_object_schema() -> None:
    context, _ = _make_context()
    tool = create_upsert_stakeholder(context, lambda: None)  # type: ignore[arg-type]

    params = inspect.signature(tool).parameters
    for field_name in ("stance", "decision_power", "temperature"):
        annotation = str(params[field_name].annotation)
        assert "dict" not in annotation


def test_registry_uses_only_upsert_for_analysis_agents() -> None:
    tool_values = {tool.value for tool in ToolName}
    assert "upsert_stakeholder" in tool_values
    assert "create_stakeholder" not in tool_values
    assert "update_stakeholder" not in tool_values

    agent_names = (
        "BriefingAgent",
        "SummaryAgent",
        "DealSummaryAgent",
        "RiskBlockerExtractionAgent",
        "StakeholderAnalysisAgent",
    )
    for agent_name in agent_names:
        tools = AGENT_TOOLS[agent_name]
        assert ToolName.UPSERT_STAKEHOLDER in tools
