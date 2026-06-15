from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from src.agents.context import AgentContext
from src.agents.tools.crm.add_stakeholder import (
    add_stakeholder_direct,
    create_add_stakeholder,
)
from src.service.crm.insights.stakeholders_service import StakeholdersServiceError


class _AsyncSessionContext:
    def __init__(self, session: MagicMock) -> None:
        self.session = session

    async def __aenter__(self) -> MagicMock:
        return self.session

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _context(*, channel: str = "teams") -> AgentContext:
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        user_email="seller@copilotgtm.com",
        channel=channel,
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(MagicMock()))
    return context


def _stakeholder(*, stakeholder_id: UUID | None = None, deal_id: UUID | None = None):
    participant_id = stakeholder_id or uuid4()
    return SimpleNamespace(
        participant_id=participant_id,
        deal_id=deal_id or uuid4(),
        email="buyer@acme.com",
        name="Buyer Person",
        job_title="VP Sales",
        phone_number="+1 555 0100",
        stance="CHAMPION",
        decision_power="DECISION_MAKER",
        approval_status="APPROVED",
    )


@pytest.mark.asyncio
async def test_add_stakeholder_direct_requires_email() -> None:
    result = await add_stakeholder_direct(
        context=_context(),
        deal_id=str(uuid4()),
        deal_name="Acme Expansion",
        email=" ",
    )

    assert result["status"] == "error"
    assert "email" in result["message"].lower()


@pytest.mark.asyncio
async def test_add_stakeholder_direct_rejects_invalid_deal_id() -> None:
    result = await add_stakeholder_direct(
        context=_context(),
        deal_id="not-a-uuid",
        deal_name="Acme Expansion",
        email="buyer@acme.com",
    )

    assert result["status"] == "error"
    assert "deal" in result["message"].lower()


@pytest.mark.asyncio
async def test_add_stakeholder_direct_rejects_invalid_enum_values() -> None:
    result = await add_stakeholder_direct(
        context=_context(),
        deal_id=str(uuid4()),
        deal_name="Acme Expansion",
        email="buyer@acme.com",
        stance="VERY_EXCITED",
    )

    assert result == {
        "status": "error",
        "message": "Invalid stance 'VERY_EXCITED'. Use one of: UNKNOWN, CHAMPION, SUPPORTER, NEUTRAL, DETRACTOR, BLOCKER.",
    }


@pytest.mark.asyncio
async def test_add_stakeholder_direct_rejects_internal_email_service_error() -> None:
    with patch("src.agents.tools.crm.add_stakeholder.StakeholdersService") as service_cls:
        service = MagicMock()
        service.create_stakeholder = AsyncMock(
            side_effect=StakeholdersServiceError(
                "This email belongs to an internal team member and cannot be added "
                "as a stakeholder. Stakeholders must be external contacts only.",
                status_code=403,
            )
        )
        service_cls.return_value = service

        result = await add_stakeholder_direct(
            context=_context(),
            deal_id=str(uuid4()),
            deal_name="Acme Expansion",
            email="seller@copilotgtm.com",
        )

    assert result["status"] == "error"
    assert "internal team member" in result["message"].lower()


@pytest.mark.asyncio
async def test_add_stakeholder_direct_creates_stakeholder_and_dispatches_jobs() -> None:
    context = _context()
    org_session = MagicMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    deal_id = uuid4()
    stakeholder_id = uuid4()
    stakeholder = _stakeholder(stakeholder_id=stakeholder_id, deal_id=deal_id)
    processing_jobs = [
        SimpleNamespace(job_id=uuid4(), job_type="DEAL_EMAIL_REMATCH", status="PENDING"),
        SimpleNamespace(job_id=uuid4(), job_type="DEAL_CALENDAR_REMATCH", status="PENDING"),
    ]
    research_job_id = uuid4()

    with (
        patch("src.agents.tools.crm.add_stakeholder.StakeholdersService") as service_cls,
        patch(
            "src.agents.tools.crm.add_stakeholder.StakeholderJobVisibilityService"
        ) as job_service_cls,
        patch(
            "src.agents.tools.crm.add_stakeholder.dispatch_stakeholder_research_async",
            AsyncMock(return_value=research_job_id),
        ) as research_dispatch,
    ):
        service = MagicMock()
        service.create_stakeholder = AsyncMock(return_value=stakeholder)
        service_cls.return_value = service
        job_service = MagicMock()
        job_service.create_stakeholder_processing_jobs = AsyncMock(return_value=processing_jobs)
        job_service_cls.return_value = job_service

        result = await add_stakeholder_direct(
            context=context,
            deal_id=str(deal_id),
            deal_name=" Acme Expansion ",
            email=" BUYER@ACME.COM ",
            name=" Buyer Person ",
            job_title=" VP Sales ",
            phone_number=" +1 555 0100 ",
            stance="CHAMPION",
            decision_power="DECISION_MAKER",
            motivation="Expansion project",
            pain_points=["Manual reporting"],
        )

    service_cls.assert_called_once_with(org_session, context.organisation_id, context.user_id)
    request = service.create_stakeholder.await_args.args[1]
    assert request.email == "BUYER@ACME.COM"
    assert request.name == "Buyer Person"
    assert request.job_title == "VP Sales"
    assert request.phone_number == "+1 555 0100"
    assert request.stance.value == "CHAMPION"
    assert request.decision_power.value == "DECISION_MAKER"
    assert request.external_links is None
    job_service_cls.assert_called_once_with(org_session, context.organisation_id, context.user_id)
    job_service.create_stakeholder_processing_jobs.assert_awaited_once_with(deal_id, stakeholder_id)
    research_dispatch.assert_awaited_once_with(
        organisation_id=context.organisation_id,
        stakeholder_id=stakeholder_id,
        deal_id=deal_id,
        user_id=context.user_id,
    )
    assert result["status"] == "created"
    assert result["participant_id"] == str(stakeholder_id)
    assert "stakeholder_id" not in result
    assert result["deal_id"] == str(deal_id)
    assert result["deal_name"] == "Acme Expansion"
    assert "champion_potential" not in result
    assert len(result["processing_jobs"]) == 2
    assert result["research_job"] == {
        "job_id": str(research_job_id),
        "job_type": "stakeholder_research",
        "status": "pending",
    }
    assert result["post_create_jobs"] == {
        "processing": {"status": "queued"},
        "research": {"status": "queued"},
    }
    assert result["_bot_channel_payload"]["type"] == "message"


@pytest.mark.asyncio
async def test_add_stakeholder_direct_returns_success_when_processing_jobs_fail() -> None:
    context = _context(channel="web")
    deal_id = uuid4()
    stakeholder = _stakeholder(deal_id=deal_id)
    research_job_id = uuid4()

    with (
        patch("src.agents.tools.crm.add_stakeholder.StakeholdersService") as service_cls,
        patch(
            "src.agents.tools.crm.add_stakeholder.StakeholderJobVisibilityService"
        ) as job_service_cls,
        patch(
            "src.agents.tools.crm.add_stakeholder.dispatch_stakeholder_research_async",
            AsyncMock(return_value=research_job_id),
        ) as research_dispatch,
    ):
        service = MagicMock()
        service.create_stakeholder = AsyncMock(return_value=stakeholder)
        service_cls.return_value = service
        job_service = MagicMock()
        job_service.create_stakeholder_processing_jobs = AsyncMock(
            side_effect=RuntimeError("queue down")
        )
        job_service_cls.return_value = job_service

        result = await add_stakeholder_direct(
            context=context,
            deal_id=str(deal_id),
            deal_name="Acme Expansion",
            email="buyer@acme.com",
        )

    research_dispatch.assert_awaited_once()
    assert result["status"] == "created"
    assert result["processing_jobs"] == []
    assert result["research_job"]["job_id"] == str(research_job_id)
    assert result["post_create_jobs"]["processing"] == {
        "status": "failed",
        "error": "queue down",
    }
    assert result["post_create_jobs"]["research"] == {"status": "queued"}


@pytest.mark.asyncio
async def test_add_stakeholder_direct_returns_success_when_research_dispatch_fails() -> None:
    context = _context(channel="web")
    deal_id = uuid4()
    stakeholder = _stakeholder(deal_id=deal_id)
    processing_jobs = [
        SimpleNamespace(job_id=uuid4(), job_type="DEAL_EMAIL_REMATCH", status="PENDING"),
    ]

    with (
        patch("src.agents.tools.crm.add_stakeholder.StakeholdersService") as service_cls,
        patch(
            "src.agents.tools.crm.add_stakeholder.StakeholderJobVisibilityService"
        ) as job_service_cls,
        patch(
            "src.agents.tools.crm.add_stakeholder.dispatch_stakeholder_research_async",
            AsyncMock(side_effect=RuntimeError("broker down")),
        ),
    ):
        service = MagicMock()
        service.create_stakeholder = AsyncMock(return_value=stakeholder)
        service_cls.return_value = service
        job_service = MagicMock()
        job_service.create_stakeholder_processing_jobs = AsyncMock(return_value=processing_jobs)
        job_service_cls.return_value = job_service

        result = await add_stakeholder_direct(
            context=context,
            deal_id=str(deal_id),
            deal_name="Acme Expansion",
            email="buyer@acme.com",
        )

    assert result["status"] == "created"
    assert len(result["processing_jobs"]) == 1
    assert result["research_job"] is None
    assert result["post_create_jobs"]["processing"] == {"status": "queued"}
    assert result["post_create_jobs"]["research"] == {
        "status": "failed",
        "error": "broker down",
    }


@pytest.mark.asyncio
async def test_add_stakeholder_direct_preserves_conflict_data() -> None:
    conflict = {
        "stakeholder_id": str(uuid4()),
        "approval_status": "APPROVED",
        "is_deleted": False,
    }

    with patch("src.agents.tools.crm.add_stakeholder.StakeholdersService") as service_cls:
        service = MagicMock()
        service.create_stakeholder = AsyncMock(
            side_effect=StakeholdersServiceError(
                "A stakeholder with this email already exists for this deal",
                status_code=409,
                data=conflict,
            )
        )
        service_cls.return_value = service

        result = await add_stakeholder_direct(
            context=_context(),
            deal_id=str(uuid4()),
            deal_name="Acme Expansion",
            email="buyer@acme.com",
        )

    assert result["status"] == "error"
    assert result["conflict"] == conflict


@pytest.mark.asyncio
async def test_add_stakeholder_tool_hides_channel_payload_from_public_result() -> None:
    context = _context()
    tool = create_add_stakeholder(context, lambda: None)

    with patch("src.agents.tools.crm.add_stakeholder.add_stakeholder_direct") as direct:
        direct.return_value = {
            "status": "created",
            "stakeholder_id": str(uuid4()),
            "_bot_channel_payload": {"type": "message", "content": {"text": "card"}},
        }

        result = await tool(
            deal_id=str(uuid4()),
            deal_name="Acme Expansion",
            email="buyer@acme.com",
        )

    assert result is not None
    assert result["status"] == "created"
    assert "_bot_channel_payload" not in result


@pytest.mark.asyncio
async def test_add_stakeholder_direct_builds_slack_success_payload() -> None:
    context = _context(channel="slack")
    deal_id = uuid4()
    stakeholder = _stakeholder(deal_id=deal_id)

    with (
        patch("src.agents.tools.crm.add_stakeholder.StakeholdersService") as service_cls,
        patch(
            "src.agents.tools.crm.add_stakeholder.StakeholderJobVisibilityService"
        ) as job_service_cls,
        patch(
            "src.agents.tools.crm.add_stakeholder.dispatch_stakeholder_research_async",
            AsyncMock(return_value=None),
        ),
    ):
        service = MagicMock()
        service.create_stakeholder = AsyncMock(return_value=stakeholder)
        service_cls.return_value = service
        job_service = MagicMock()
        job_service.create_stakeholder_processing_jobs = AsyncMock(return_value=[])
        job_service_cls.return_value = job_service

        result = await add_stakeholder_direct(
            context=context,
            deal_id=str(deal_id),
            deal_name="Acme Expansion",
            email="buyer@acme.com",
        )

    payload = result["_bot_channel_payload"]
    assert payload["type"] == "message"
    assert payload["content"]["blocks"][0]["type"] == "header"
    assert payload["content"]["blocks"][0]["text"]["text"] == "Stakeholder added"
    assert payload["text_fallback"] == "Added Buyer Person as a stakeholder to Acme Expansion."
