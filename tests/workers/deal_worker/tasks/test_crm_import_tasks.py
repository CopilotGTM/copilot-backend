import importlib.util
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.crm.imports import CRMImportRowStatus
from src.service.crm.imports.import_service import CRMImportService
from src.workers.deal_worker.config import DealWorkerConfig


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _ScalarRows(self._rows)


class _Session:
    def __init__(self, *execute_rows):
        self.execute_rows = list(execute_rows)
        self.added = []
        self.flush_count = 0
        self.commit_count = 0

    async def execute(self, _stmt):
        return _Result(self.execute_rows.pop(0) if self.execute_rows else [])

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        self.flush_count += 1

    async def commit(self):
        self.commit_count += 1


class _BrokenStorage:
    async def get_blob_content(self, _blob_path):
        raise ValueError("bad csv bytes")


def _load_task_module():
    module_name = "test_crm_import_tasks_runtime"
    module_path = "src/workers/deal_worker/tasks/crm_import_tasks.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _job(*, org_id, user_id, metadata=None, status=JobStatus.PENDING):
    return DealJobs(
        organisation_id=org_id,
        user_id=user_id,
        job_type=DealJobType.CRM_CSV_IMPORT,
        deal_id=None,
        status=status,
        job_metadata=metadata or {},
    )


@pytest.mark.asyncio
async def test_worker_scoped_service_loads_import_without_user_id_and_uses_job_actor(monkeypatch):
    org_id = uuid4()
    user_id = uuid4()
    job = _job(
        org_id=org_id,
        user_id=user_id,
        metadata={"mapping": {"Email": "contact.email"}},
        status=JobStatus.IN_PROGRESS,
    )
    row = SimpleNamespace(
        raw_values={"Email": "buyer@example.com"},
        status=CRMImportRowStatus.PREVIEW,
        account_id=None,
        contact_id=None,
        error_code=None,
        error_message=None,
    )
    session = _Session([job], [row])
    seen_actor_ids = []

    async def fake_process_account(self, row, mapping, account_writer):
        seen_actor_ids.append(self._current_actor_user_id)
        return None, False

    async def fake_process_contact(self, row, mapping, contact_writer, *, account_id):
        seen_actor_ids.append(self._current_actor_user_id)
        return uuid4(), True

    monkeypatch.setattr(CRMImportService, "_process_account", fake_process_account)
    monkeypatch.setattr(CRMImportService, "_process_contact", fake_process_contact)

    service = CRMImportService(session, org_id, user_id=None)
    await service.process_next_batch(job.job_id)

    assert seen_actor_ids == [user_id, user_id]


@pytest.mark.asyncio
async def test_analyze_csv_marks_failed_job_on_non_transient_storage_error(monkeypatch):
    crm_import_tasks = _load_task_module()

    org_id = uuid4()
    user_id = uuid4()
    import_job_id = uuid4()
    failed = {}

    async def fake_record_import_failure(*, organisation_id, import_job_id, error):
        failed.update(
            {
                "organisation_id": organisation_id,
                "import_job_id": import_job_id,
                "error": error,
            }
        )

    monkeypatch.setattr(crm_import_tasks, "_record_import_failure", fake_record_import_failure)

    class Service:
        def __init__(self, *_args, **_kwargs):
            pass

        async def analyze(self, _import_job_id):
            raise ValueError("bad csv bytes")

    async def fake_run_with_org_session(_organisation_id, operation):
        return await operation("session")

    monkeypatch.setattr(crm_import_tasks, "CRMImportService", Service)
    monkeypatch.setattr(
        crm_import_tasks,
        "_run_with_org_session",
        AsyncMock(side_effect=fake_run_with_org_session),
    )

    result = await crm_import_tasks.analyze_csv_import(org_id, import_job_id)

    assert result["status"] == "failed"
    assert failed == {
        "organisation_id": org_id,
        "import_job_id": import_job_id,
        "error": "bad csv bytes",
    }
    assert user_id


@pytest.mark.asyncio
async def test_process_csv_marks_failed_job_on_top_level_processing_error(monkeypatch):
    crm_import_tasks = _load_task_module()

    org_id = uuid4()
    import_job_id = uuid4()
    failed = {}

    async def fake_record_import_failure(*, organisation_id, import_job_id, error):
        failed.update(
            {
                "organisation_id": organisation_id,
                "import_job_id": import_job_id,
                "error": error,
            }
        )

    class Service:
        def __init__(self, *_args, **_kwargs):
            pass

        async def process_next_batch(self, _import_job_id):
            raise RuntimeError("database unavailable")

    async def fake_run_with_org_and_master_session(_organisation_id, operation):
        return await operation("org-session", "master-session")

    monkeypatch.setattr(crm_import_tasks, "_record_import_failure", fake_record_import_failure)
    monkeypatch.setattr(crm_import_tasks, "CRMImportService", Service)
    monkeypatch.setattr(
        crm_import_tasks,
        "_run_with_org_and_master_session",
        AsyncMock(side_effect=fake_run_with_org_and_master_session),
    )

    result = await crm_import_tasks.process_csv_import(org_id, import_job_id)

    assert result["status"] == "failed"
    assert failed == {
        "organisation_id": org_id,
        "import_job_id": import_job_id,
        "error": "database unavailable",
    }


def test_crm_import_tasks_are_registered_on_deal_queue():
    crm_import_tasks = _load_task_module()

    assert crm_import_tasks.analyze_csv_import_task.name == "crm_import.analyze_csv"
    assert crm_import_tasks.process_csv_import_task.name == "crm_import.process_csv"
    assert DealWorkerConfig().task_routes["crm_import.analyze_csv"]["queue"] == "deal"
    assert DealWorkerConfig().task_routes["crm_import.process_csv"]["queue"] == "deal"
