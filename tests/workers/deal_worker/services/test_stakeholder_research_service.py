"""Tests for stakeholder research service internals."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import src.workers.deal_worker.services.deal_insight_services.stakeholder_research_service as service_module
from src.agents.workflows.stakeholder_research.models import StakeholderResearchResult
from src.workers.deal_worker.services.deal_insight_services.stakeholder_research_service import (
    StakeholderResearchService,
)


class _FakeSession:
    def __init__(self, contact: SimpleNamespace):
        self._contact = contact
        self.execute = AsyncMock()
        self.commit = AsyncMock()

    async def get(self, _model, _contact_id):  # noqa: ANN001 - model type is not relevant here
        return self._contact


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
