from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.job_models import DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.workers.deal_worker.config import DealWorkerConfig
from src.workers.deal_worker.scheduled_jobs import SCHEDULED_JOBS


class _Result:
    def __init__(self, rows=None):
        self.rows = rows or []

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _Session:
    def __init__(self, contact, active_jobs=None):
        self.contact = contact
        self.active_jobs = active_jobs or []
        self.added = []
        self.commits = 0

    async def get(self, _model, _item_id):  # noqa: ANN001
        return self.contact

    async def execute(self, _stmt):  # noqa: ANN001
        return _Result(self.active_jobs)

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        self.commits += 1


class _Projection:
    def __init__(self, _session, _organisation_id):  # noqa: ANN001
        self.calls = []
        PROJECTIONS.append(self)

    async def upsert_contact_enrichment_triggered(self, contact, **kwargs):  # noqa: ANN001
        self.calls.append((contact, kwargs))


PROJECTIONS: list[_Projection] = []


class _SessionSource:
    def __init__(self, session):
        self.session = session

    async def __aiter__(self):
        yield self.session


@pytest.mark.asyncio
async def test_dispatch_skips_free_provider_subdomain(monkeypatch) -> None:
    """Automatic contact research should not enqueue jobs for personal email domains."""
    import src.workers.deal_worker.utils.contact_research_dispatch as dispatch_module

    organisation_id = uuid4()
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="nick@enter.gmail.com",
    )
    session = _Session(contact)
    sent = []

    monkeypatch.setattr(
        dispatch_module,
        "acquire_org_async_session",
        lambda _organisation_id: _SessionSource(session),
    )
    monkeypatch.setattr(dispatch_module, "send_deal_task_async", sent.append)

    job_id = await dispatch_module.dispatch_contact_research_async(
        organisation_id=organisation_id,
        contact_id=contact.contact_id,
    )

    assert job_id is None
    assert session.added == []
    assert sent == []


@pytest.mark.asyncio
async def test_dispatch_reuses_active_contact_job(monkeypatch) -> None:
    """Active jobs keyed by contact_id are the idempotency boundary."""
    import src.workers.deal_worker.utils.contact_research_dispatch as dispatch_module

    organisation_id = uuid4()
    contact_id = uuid4()
    existing_job = SimpleNamespace(
        job_id=uuid4(),
        job_metadata={"contact_id": str(contact_id)},
        status=JobStatus.PENDING,
    )
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        contact_id=contact_id,
        email="buyer@acme.com",
    )
    session = _Session(contact, active_jobs=[existing_job])
    sent = []

    monkeypatch.setattr(
        dispatch_module,
        "acquire_org_async_session",
        lambda _organisation_id: _SessionSource(session),
    )
    monkeypatch.setattr(dispatch_module, "send_deal_task_async", sent.append)
    PROJECTIONS.clear()
    monkeypatch.setattr(dispatch_module, "ActivityFeedProjectionService", _Projection)

    job_id = await dispatch_module.dispatch_contact_research_async(
        organisation_id=organisation_id,
        contact_id=contact_id,
    )

    assert job_id == str(existing_job.job_id)
    assert session.added == []
    assert sent == []
    assert PROJECTIONS == []


@pytest.mark.asyncio
async def test_dispatch_creates_contact_research_job(monkeypatch) -> None:
    """Business-domain contacts should enqueue a contact-scoped research job."""
    import src.workers.deal_worker.utils.contact_research_dispatch as dispatch_module

    organisation_id = uuid4()
    user_id = uuid4()
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="buyer@test.hackme.com",
    )
    session = _Session(contact)
    sent: list[tuple[str, dict]] = []

    async def fake_send(task_name, **kwargs):  # noqa: ANN001
        sent.append((task_name, kwargs))

    monkeypatch.setattr(
        dispatch_module,
        "acquire_org_async_session",
        lambda _organisation_id: _SessionSource(session),
    )
    monkeypatch.setattr(dispatch_module, "send_deal_task_async", fake_send)
    PROJECTIONS.clear()
    monkeypatch.setattr(dispatch_module, "ActivityFeedProjectionService", _Projection)

    job_id = await dispatch_module.dispatch_contact_research_async(
        organisation_id=organisation_id,
        contact_id=contact.contact_id,
        user_id=user_id,
        trigger="contact_created",
    )

    assert job_id is not None
    assert len(session.added) == 1
    job = session.added[0]
    assert job.job_type == DealJobType.CONTACT_RESEARCH
    assert job.user_id == user_id
    assert job.job_metadata["contact_id"] == str(contact.contact_id)
    assert job.job_metadata["normalized_email"] == "buyer@test.hackme.com"
    assert job.job_metadata["trigger"] == "contact_created"
    assert len(PROJECTIONS) == 1
    assert PROJECTIONS[0].calls == [
        (
            contact,
            {
                "job_id": job.job_id,
                "actor_user_id": user_id,
            },
        )
    ]
    assert session.commits == 1
    assert sent == [
        (
            "contact_research.process_job",
            {
                "kwargs": {
                    "organisation_id": str(organisation_id),
                    "job_id": str(job.job_id),
                },
                "queue": "deal_jobs",
            },
        )
    ]


def test_contact_research_has_no_scheduled_retry_route() -> None:
    assert "contact-research-retry" not in SCHEDULED_JOBS
    routes = DealWorkerConfig().task_routes
    assert "contact_research.process_job" in routes
    assert "contact_research.retry_pending_org" not in routes
    assert "contact_research.trigger_retry_all_orgs" not in routes
