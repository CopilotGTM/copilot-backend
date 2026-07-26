"""Regression tests for the shared contact research dispatch boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.workers.deal_worker.config import DealWorkerConfig
from src.workers.deal_worker.scheduled_jobs import SCHEDULED_JOBS


class _Result:
    def __init__(self, rows=None):
        self.rows = rows or []

    def scalars(self):
        """Return self so tests can mimic SQLAlchemy's scalar result chain."""
        return self

    def all(self):
        """Return all queued rows for active-job lookup assertions."""
        return self.rows

    def first(self):
        """Return the first row for locked contact lookup assertions."""
        return self.rows[0] if self.rows else None


class _Session:
    def __init__(self, contact, active_jobs=None):
        self.contact = contact
        self.active_jobs = active_jobs or []
        self.added = []
        self.commits = 0

    async def get(self, model, item_id, **_kwargs):  # noqa: ANN001
        """Return the configured contact or a job persisted earlier in the test."""
        if model is DealJobs:
            return next((job for job in self.added if job.job_id == item_id), None)
        return self.contact

    async def execute(self, _stmt):  # noqa: ANN001
        """Return the contact for its locked query, otherwise active jobs."""
        entity = _stmt.column_descriptions[0].get("entity")
        if entity is CRMContact:
            return _Result([self.contact] if self.contact else [])
        return _Result(self.active_jobs)

    def add(self, row):
        """Capture the job row that dispatch should persist."""
        self.added.append(row)

    async def commit(self):
        """Record commits so tests prove job creation persists once."""
        self.commits += 1


class _Projection:
    def __init__(self, _session, _organisation_id):  # noqa: ANN001
        self.calls = []
        PROJECTIONS.append(self)

    async def upsert_contact_enrichment_triggered(self, contact, **kwargs):  # noqa: ANN001
        """Record projection calls without touching activity-feed storage."""
        self.calls.append((contact, kwargs))


PROJECTIONS: list[_Projection] = []


class _SessionSource:
    def __init__(self, session):
        self.session = session

    async def __aiter__(self):
        yield self.session


@pytest.mark.asyncio
async def test_dispatch_for_contact_skips_missing_contact_id(monkeypatch) -> None:
    """Post-commit callers without a linked contact should not open a DB session."""
    import src.workers.deal_worker.utils.contact_research_dispatch as dispatch_module

    def fail_session(_organisation_id):  # noqa: ANN001
        """Fail if a missing contact enters DB-backed dispatch logic."""
        raise AssertionError("missing contact_id should skip dispatch before session acquisition")

    monkeypatch.setattr(dispatch_module, "acquire_org_async_session", fail_session)

    job_id = await dispatch_module.dispatch_contact_research_for_contact(
        organisation_id=uuid4(),
        contact_id=None,
        trigger="stakeholder_created",
    )

    assert job_id is None


@pytest.mark.asyncio
async def test_dispatch_for_contact_delegates_to_contact_research(monkeypatch) -> None:
    """Linked contact IDs should flow through the shared dispatcher boundary."""
    import src.workers.deal_worker.utils.contact_research_dispatch as dispatch_module

    organisation_id = uuid4()
    contact_id = uuid4()
    user_id = uuid4()
    calls = []

    async def fake_dispatch_contact_research_async(**kwargs):  # noqa: ANN001
        """Capture low-level dispatcher kwargs for boundary assertions."""
        calls.append(kwargs)
        return "queued-job"

    monkeypatch.setattr(
        dispatch_module,
        "dispatch_contact_research_async",
        fake_dispatch_contact_research_async,
    )

    job_id = await dispatch_module.dispatch_contact_research_for_contact(
        organisation_id=organisation_id,
        contact_id=contact_id,
        user_id=user_id,
        trigger="stakeholder_created",
        force_refresh=True,
    )

    assert job_id == "queued-job"
    assert calls == [
        {
            "organisation_id": organisation_id,
            "contact_id": contact_id,
            "user_id": user_id,
            "trigger": "stakeholder_created",
            "force_refresh": True,
        }
    ]


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
async def test_dispatch_accepts_linkedin_after_email_miss(monkeypatch) -> None:
    """A LinkedIn URL should make a prior personal-email miss researchable."""
    import src.workers.deal_worker.utils.contact_research_dispatch as dispatch_module

    organisation_id = uuid4()
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="buyer@gmail.com",
        linkedin_url="https://linkedin.com/in/buyer",
        profile_enrichment={"provider": "apollo", "status": "not_found"},
    )
    session = _Session(contact)

    async def fake_send(*_args, **_kwargs):
        return None

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
        force_refresh=True,
    )

    assert job_id is not None
    assert session.added[0].job_metadata["normalized_email"] == "buyer@gmail.com"
    assert session.added[0].job_metadata["linkedin_url"] == contact.linkedin_url


@pytest.mark.asyncio
async def test_force_refresh_does_not_reuse_job_with_older_identity(monkeypatch) -> None:
    """An active email-only job must not swallow a retry after LinkedIn is added."""
    import src.workers.deal_worker.utils.contact_research_dispatch as dispatch_module

    organisation_id = uuid4()
    contact_id = uuid4()
    existing_job = SimpleNamespace(
        job_id=uuid4(),
        job_metadata={
            "contact_id": str(contact_id),
            "normalized_email": "buyer@acme.com",
            "linkedin_url": None,
        },
        status=JobStatus.IN_PROGRESS,
    )
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        contact_id=contact_id,
        email="buyer@acme.com",
        linkedin_url="https://linkedin.com/in/buyer",
    )
    session = _Session(contact, active_jobs=[existing_job])

    async def fake_send(*_args, **_kwargs):
        return None

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
        contact_id=contact_id,
        force_refresh=True,
    )

    assert job_id != str(existing_job.job_id)
    assert len(session.added) == 1


@pytest.mark.asyncio
async def test_force_refresh_does_not_reuse_a_non_refresh_job(monkeypatch) -> None:
    """Manual refresh must not inherit a cache-using job created by a detail-page race."""
    import src.workers.deal_worker.utils.contact_research_dispatch as dispatch_module

    organisation_id = uuid4()
    contact_id = uuid4()
    existing_job = SimpleNamespace(
        job_id=uuid4(),
        job_metadata={
            "contact_id": str(contact_id),
            "normalized_email": "buyer@acme.com",
            "linkedin_url": "https://linkedin.com/in/buyer",
            "force_refresh": False,
        },
        status=JobStatus.PENDING,
    )
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        contact_id=contact_id,
        email="buyer@acme.com",
        linkedin_url="https://linkedin.com/in/buyer",
    )
    session = _Session(contact, active_jobs=[existing_job])

    async def fake_send(*_args, **_kwargs):
        """Accept the forced replacement without contacting the broker."""
        return None

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
        contact_id=contact_id,
        force_refresh=True,
    )

    assert job_id != str(existing_job.job_id)
    assert session.added[0].job_metadata["force_refresh"] is True


@pytest.mark.asyncio
async def test_dispatch_does_not_return_another_users_active_job(monkeypatch) -> None:
    """A visible job returned to one user must pass that user's stream authorization."""
    import src.workers.deal_worker.utils.contact_research_dispatch as dispatch_module

    organisation_id = uuid4()
    contact_id = uuid4()
    current_user_id = uuid4()
    existing_job = SimpleNamespace(
        job_id=uuid4(),
        user_id=uuid4(),
        job_metadata={
            "contact_id": str(contact_id),
            "normalized_email": "buyer@acme.com",
            "linkedin_url": None,
            "force_refresh": False,
        },
        status=JobStatus.IN_PROGRESS,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        contact_id=contact_id,
        email="buyer@acme.com",
    )
    session = _Session(contact, active_jobs=[existing_job])

    async def fake_send(*_args, **_kwargs):
        """Accept the current user's replacement without contacting the broker."""
        return None

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
        contact_id=contact_id,
        user_id=current_user_id,
    )

    assert job_id != str(existing_job.job_id)
    assert session.added[0].user_id == current_user_id


@pytest.mark.asyncio
async def test_dispatch_replaces_an_abandoned_active_job(monkeypatch) -> None:
    """A stale pending row must not prevent recovery after queue publication was interrupted."""
    import src.workers.deal_worker.utils.contact_research_dispatch as dispatch_module

    organisation_id = uuid4()
    contact_id = uuid4()
    stale_at = datetime.now(UTC) - timedelta(minutes=10)
    existing_job = SimpleNamespace(
        job_id=uuid4(),
        job_metadata={
            "contact_id": str(contact_id),
            "normalized_email": "buyer@acme.com",
            "linkedin_url": None,
        },
        status=JobStatus.PENDING,
        created_at=stale_at,
        updated_at=stale_at,
    )
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        contact_id=contact_id,
        email="buyer@acme.com",
    )
    session = _Session(contact, active_jobs=[existing_job])

    async def fake_send(*_args, **_kwargs):
        """Accept the replacement job without contacting the broker."""
        return None

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
        contact_id=contact_id,
        force_refresh=True,
    )

    assert job_id != str(existing_job.job_id)
    assert len(session.added) == 1


@pytest.mark.asyncio
async def test_dispatch_marks_job_failed_when_queue_publication_fails(monkeypatch) -> None:
    """A broker failure must be terminal so detail polling does not spin forever."""
    import src.workers.deal_worker.utils.contact_research_dispatch as dispatch_module

    organisation_id = uuid4()
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="buyer@acme.com",
    )
    session = _Session(contact)

    async def fail_send(*_args, **_kwargs):
        """Simulate the broker rejecting the newly persisted job."""
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(
        dispatch_module,
        "acquire_org_async_session",
        lambda _organisation_id: _SessionSource(session),
    )
    monkeypatch.setattr(dispatch_module, "send_deal_task_async", fail_send)
    PROJECTIONS.clear()
    monkeypatch.setattr(dispatch_module, "ActivityFeedProjectionService", _Projection)

    with pytest.raises(RuntimeError, match="broker unavailable"):
        await dispatch_module.dispatch_contact_research_async(
            organisation_id=organisation_id,
            contact_id=contact.contact_id,
        )

    assert session.added[0].status == JobStatus.FAILED
    assert session.added[0].error_message == "Failed to enqueue contact research"


@pytest.mark.asyncio
async def test_dispatch_reuses_active_contact_job(monkeypatch) -> None:
    """An active job with the current identities is the idempotency boundary."""
    import src.workers.deal_worker.utils.contact_research_dispatch as dispatch_module

    organisation_id = uuid4()
    contact_id = uuid4()
    existing_job = SimpleNamespace(
        job_id=uuid4(),
        job_metadata={
            "contact_id": str(contact_id),
            "normalized_email": "buyer@acme.com",
            "linkedin_url": None,
        },
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
async def test_dispatch_queues_summary_when_provider_enriched_but_research_missing(
    monkeypatch,
) -> None:
    """Fresh Apollo enrichment should not block the missing LLM research summary."""
    import src.workers.deal_worker.utils.contact_research_dispatch as dispatch_module

    organisation_id = uuid4()
    user_id = uuid4()
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="buyer@acme.com",
        profile_enrichment={"provider": "apollo", "headline": "Revenue leader"},
        profile_enriched_at=datetime(2026, 7, 2, 10, 0, tzinfo=UTC),
        last_researched_at=datetime(2026, 7, 2, 10, 1, tzinfo=UTC),
        contact_research=None,
    )
    session = _Session(contact)
    sent: list[tuple[str, dict]] = []

    async def fake_send(task_name, **kwargs):  # noqa: ANN001
        """Capture broker sends without requiring a running worker queue."""
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
    assert sent[0][0] == "contact_research.process_job"


@pytest.mark.asyncio
async def test_dispatch_queues_summary_for_provider_enriched_contact_without_email(
    monkeypatch,
) -> None:
    """LinkedIn-created contacts with cached provider data can still get LLM summaries."""
    import src.workers.deal_worker.utils.contact_research_dispatch as dispatch_module

    organisation_id = uuid4()
    user_id = uuid4()
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email=None,
        linkedin_url="https://linkedin.com/in/no-email",
        profile_enrichment={"provider": "apollo", "name": "No Email Buyer"},
        contact_research=None,
    )
    session = _Session(contact)
    sent: list[tuple[str, dict]] = []

    async def fake_send(task_name, **kwargs):  # noqa: ANN001
        """Capture broker sends without requiring a running worker queue."""
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
    assert session.added[0].job_metadata["normalized_email"] is None
    assert sent[0][0] == "contact_research.process_job"


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
        """Capture broker sends without requiring a running worker queue."""
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


@pytest.mark.asyncio
async def test_dispatch_skips_recent_summary_only_when_prompt_and_payload_are_current(
    monkeypatch,
) -> None:
    """Prompt upgrades and changed Apollo evidence must invalidate a cached summary."""
    import src.workers.deal_worker.utils.contact_research_dispatch as dispatch_module

    organisation_id = uuid4()
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="buyer@acme.com",
        contact_research="Existing complete profile",
        last_researched_at=datetime.now(UTC),
        profile_enrichment={
            "provider": "apollo",
            "_metadata": {
                "prompt_version": dispatch_module.CONTACT_RESEARCH_PROMPT_VERSION,
                "payload_hash": "same-hash",
                "summary_source_hash": "same-hash",
            },
        },
    )
    session = _Session(contact)

    monkeypatch.setattr(
        dispatch_module,
        "acquire_org_async_session",
        lambda _organisation_id: _SessionSource(session),
    )

    job_id = await dispatch_module.dispatch_contact_research_async(
        organisation_id=organisation_id,
        contact_id=contact.contact_id,
    )

    assert job_id is None
    assert session.added == []


def test_summary_freshness_rejects_legacy_prompt_and_changed_payload() -> None:
    """A cached narrative is reusable only for the current prompt and evidence hash."""
    import src.workers.deal_worker.utils.contact_research_dispatch as dispatch_module

    assert dispatch_module._has_current_summary({"provider": "apollo"}) is False
    assert (
        dispatch_module._has_current_summary(
            {
                "_metadata": {
                    "prompt_version": dispatch_module.CONTACT_RESEARCH_PROMPT_VERSION,
                    "payload_hash": "new-hash",
                    "summary_source_hash": "old-hash",
                }
            }
        )
        is False
    )


def test_contact_research_has_no_scheduled_retry_route() -> None:
    """Contact research should run only from explicit dispatch, not scheduled retries."""
    assert "contact-research-retry" not in SCHEDULED_JOBS
    routes = DealWorkerConfig().task_routes
    assert "contact_research.process_job" in routes
    assert "contact_research.retry_pending_org" not in routes
    assert "contact_research.trigger_retry_all_orgs" not in routes
