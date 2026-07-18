from contextlib import contextmanager
from uuid import uuid4

from src.domain.models.organisation_tables.job_models import DealJobType
from src.workers.deal_worker.utils import pipeline_dispatch


class _FakeSession:
    def __init__(self) -> None:
        self.added = []
        self.committed = False

    def add(self, item) -> None:
        self.added.append(item)

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.committed = True


class _FakeCelery:
    def __init__(self) -> None:
        self.signatures = []

    def signature(self, name: str, **kwargs):
        signature = {"name": name, **kwargs}
        self.signatures.append(signature)
        return signature


def test_deal_update_job_type_is_available_for_pipeline():
    assert DealJobType.DEAL_UPDATE.value == "deal_update"


def test_deal_update_pipeline_flag_defaults_disabled(monkeypatch):
    monkeypatch.delenv("ENABLE_DEAL_UPDATE_AGENT_PIPELINE", raising=False)

    assert pipeline_dispatch._deal_update_pipeline_enabled() is False


def test_deal_update_pipeline_flag_can_be_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_DEAL_UPDATE_AGENT_PIPELINE", "true")

    assert pipeline_dispatch._deal_update_pipeline_enabled() is True


def test_create_pipeline_jobs_adds_deal_update_when_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_DEAL_UPDATE_AGENT_PIPELINE", "true")
    session = _FakeSession()

    @contextmanager
    def fake_org_session_scope(_organisation_id):
        yield session

    monkeypatch.setattr(pipeline_dispatch, "org_session_scope", fake_org_session_scope)

    risk_job_id, stakeholder_job_id, task_job_id, deal_update_job_id = (
        pipeline_dispatch._create_pipeline_jobs(
            str(uuid4()), str(uuid4()), source_event_id=str(uuid4())
        )
    )

    assert risk_job_id
    assert stakeholder_job_id
    assert task_job_id
    assert deal_update_job_id
    assert [job.job_type for job in session.added] == [
        DealJobType.RISK_BLOCKER_EXTRACTION,
        DealJobType.STAKEHOLDER_ANALYSIS,
        DealJobType.DEAL_TASK_SYNC,
        DealJobType.DEAL_UPDATE,
    ]
    assert session.added[-1].job_metadata["approval_mode"] == "auto_safe_fields"
    assert session.committed is True


def test_build_deal_insight_canvas_appends_deal_update_after_task_sync(monkeypatch):
    celery = _FakeCelery()

    def fake_group(*signatures):
        return {"kind": "group", "items": list(signatures)}

    def fake_chain(*signatures):
        return {"kind": "chain", "items": list(signatures)}

    monkeypatch.setattr(pipeline_dispatch, "get_deal_celery_client", lambda: celery)
    monkeypatch.setattr(pipeline_dispatch, "group", fake_group)
    monkeypatch.setattr(pipeline_dispatch, "chain", fake_chain)

    canvas = pipeline_dispatch._build_deal_insight_canvas(
        organisation_id="org-1",
        deal_id="deal-1",
        job_id="memory-job",
        risk_job_id="risk-job",
        stakeholder_job_id="stakeholder-job",
        task_job_id="task-job",
        deal_update_job_id="update-job",
    )

    assert canvas["kind"] == "chain"
    assert [item["name"] for item in canvas["items"] if item.get("name")] == [
        "deal_memory.process_job",
        "deal_tasks.sync_from_summary",
        "deal_update.process_job",
    ]
    assert canvas["items"][1]["kind"] == "group"
    assert canvas["items"][3]["kwargs"] == {
        "organisation_id": "org-1",
        "job_id": "update-job",
    }
