"""Tests for stakeholder research dispatch visibility edge cases."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.job_models import JobStatus
from src.domain.models.organisation_tables.sync.crm.deals import CRMParticipantType
from src.domain.models.organisation_tables.sync.crm.stakeholders import ApprovalStatus
from src.workers.deal_worker.utils.stakeholder_research_dispatch import (
    AUTOMATED_PIPELINE_USER_ID,
    dispatch_stakeholder_research_async,
)


class _Result:
    """Fake SQLAlchemy result object for scalar_one_or_none calls."""

    def __init__(self, row):
        self.row = row

    def scalar_one_or_none(self):
        """Return the configured row."""
        return self.row


class _Session:
    """Fake org session for dispatch helper tests."""

    def __init__(self, *execute_rows):
        self.execute_rows = list(execute_rows)
        self.commits = 0

    async def get(self, model, item_id):
        """Dispatch should look up participants with scoped SELECT statements."""
        return None

    async def execute(self, stmt):
        """Return the next configured query result."""
        return _Result(self.execute_rows.pop(0))

    async def commit(self):
        """Track commits for ownership handoff assertions."""
        self.commits += 1


class _SessionSource:
    """Async iterator matching acquire_org_async_session usage."""

    def __init__(self, session):
        self.session = session

    async def __aiter__(self):
        """Yield one fake session."""
        yield self.session


@pytest.mark.asyncio
async def test_user_dispatch_claims_same_deal_automated_active_job(monkeypatch) -> None:
    """Returned research jobs must be owned by the requesting user for SSE access."""
    organisation_id = uuid4()
    deal_id = uuid4()
    participant_id = uuid4()
    user_id = uuid4()
    participant = SimpleNamespace(
        participant_id=participant_id,
        participant_type=CRMParticipantType.CONTACT,
        is_deleted=False,
        approval_status=ApprovalStatus.APPROVED.value,
        related_local_id=None,
        related_email="buyer@example.com",
        deal_id=deal_id,
    )
    existing_job = SimpleNamespace(
        job_id=uuid4(),
        user_id=AUTOMATED_PIPELINE_USER_ID,
        status=JobStatus.IN_PROGRESS,
        created_at=datetime.now(UTC),
        deal_id=deal_id,
        stakeholder_id=participant_id,
        job_metadata={"trigger": "stakeholder_created"},
    )
    session = _Session(participant, None, existing_job)

    monkeypatch.setattr(
        "src.workers.deal_worker.utils.stakeholder_research_dispatch.acquire_org_async_session",
        lambda organisation_id: _SessionSource(session),
    )

    returned_job_id = await dispatch_stakeholder_research_async(
        organisation_id=organisation_id,
        stakeholder_id=participant_id,
        deal_id=deal_id,
        user_id=user_id,
    )

    assert returned_job_id == str(existing_job.job_id)
    assert existing_job.user_id == user_id
    assert existing_job.deal_id == deal_id
    assert existing_job.stakeholder_id == participant_id
    assert existing_job.job_metadata["claimed_from"] == "automated_pipeline"
    assert session.commits == 1
