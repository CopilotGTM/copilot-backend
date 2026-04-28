from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from src.domain.models.organisation_tables.job_models import DealJobType, JobStatus


def _load_script():
    script_path = (
        Path(__file__).parents[2]
        / "scripts"
        / "local"
        / "development"
        / "trigger_deal_task_sync.py"
    )
    spec = importlib.util.spec_from_file_location("trigger_deal_task_sync", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_deal_task_sync_job_creates_pending_manual_job() -> None:
    script = _load_script()
    org_id = uuid4()
    deal_id = uuid4()

    job = script.build_deal_task_sync_job(organisation_id=org_id, deal_id=deal_id)

    assert job.organisation_id == org_id
    assert job.deal_id == deal_id
    assert job.job_type == DealJobType.DEAL_TASK_SYNC
    assert job.status == JobStatus.PENDING
    assert job.user_id == UUID("00000000-0000-0000-0000-000000000001")
    assert job.job_metadata == {"source": "manual_local_test"}


def test_enqueue_deal_task_sync_uses_existing_celery_task(monkeypatch) -> None:
    script = _load_script()
    calls = []

    class FakeTask:
        @staticmethod
        def apply_async(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(id="celery-task-1")

    monkeypatch.setattr(script, "sync_deal_tasks_from_summary", FakeTask)

    org_id = uuid4()
    deal_id = uuid4()
    job_id = uuid4()

    task_id = script.enqueue_deal_task_sync(
        organisation_id=org_id,
        deal_id=deal_id,
        job_id=job_id,
    )

    assert task_id == "celery-task-1"
    assert calls == [
        {
            "args": [str(org_id), str(deal_id), str(job_id)],
            "queue": "deal_jobs",
            "routing_key": "deal_jobs",
        }
    ]


def test_parser_requires_org_and_deal_ids() -> None:
    script = _load_script()
    parser = script.build_parser()

    args = parser.parse_args(
        [
            "--organisation-id",
            str(uuid4()),
            "--deal-id",
            str(uuid4()),
        ]
    )

    assert args.organisation_id
    assert args.deal_id
    assert args.job_id is None
