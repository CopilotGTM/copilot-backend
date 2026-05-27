from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.calendar import AssociationStatus
from src.service.crm.meetings.meetings_service import MeetingsService, MeetingsServiceError


class _ScalarResult:
    def __init__(self, values: list[object]):
        self._values = values

    def first(self):
        return self._values[0] if self._values else None


class _ExecuteResult:
    def __init__(self, values: list[object]):
        self._values = values

    def scalars(self) -> _ScalarResult:
        return _ScalarResult(self._values)


class _FakeSession:
    def __init__(self, execute_results: list[list[object]] | None = None, get_result=None):
        self.execute_results = execute_results or []
        self.get_result = get_result
        self.added: list[object] = []
        self.commits = 0
        self.flushes = 0

    async def execute(self, _stmt):
        return _ExecuteResult(self.execute_results.pop(0))

    async def get(self, _model, _pk):
        return self.get_result

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushes += 1

    async def refresh(self, _obj):
        return None

    async def commit(self):
        self.commits += 1


class _FakeStorage:
    def __init__(self, props_by_path: dict[str, object | None]):
        self.props_by_path = props_by_path
        self.closed = False

    async def get_blob_properties(self, path: str):
        return self.props_by_path.get(path)

    async def generate_write_sas(self, path: str, minutes: int = 15):
        return f"https://storage.example/{path}?minutes={minutes}"

    async def close(self):
        self.closed = True


def _storage_factory(props_by_path: dict[str, object | None]):
    return lambda _org_id: _FakeStorage(props_by_path)


@pytest.mark.asyncio
async def test_presign_replaces_pending_upload_when_blob_was_never_uploaded():
    org_id = uuid4()
    deal_id = uuid4()
    event_id = uuid4()
    old_path = f"transcripts/{deal_id}/{event_id}/old.docx"
    active_job = DealJobs(
        organisation_id=org_id,
        user_id=uuid4(),
        job_type=DealJobType.TRANSCRIPT_UPLOAD,
        deal_id=deal_id,
        event_id=event_id,
        status=JobStatus.PENDING,
        job_metadata={"storage_path": old_path},
    )
    session = _FakeSession(
        execute_results=[
            [SimpleNamespace(status=AssociationStatus.ACTIVE)],
            [],
            [active_job],
        ]
    )
    service = MeetingsService(session, org_id, storage_factory=_storage_factory({old_path: None}))

    job, upload_url, expires_in = await service.presign_transcript_upload(
        deal_id=deal_id,
        event_id=event_id,
        user_id=uuid4(),
        filename="new.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=123,
    )

    assert active_job.status == JobStatus.FAILED
    assert (
        active_job.error_message
        == "Transcript file was not uploaded to storage. Please retry the upload."
    )
    assert job is not active_job
    assert job in session.added
    assert upload_url.startswith("https://storage.example/")
    assert expires_in == 900
    assert session.commits == 1


@pytest.mark.asyncio
async def test_finalize_fails_pending_upload_when_blob_is_missing():
    org_id = uuid4()
    deal_id = uuid4()
    event_id = uuid4()
    storage_path = f"transcripts/{deal_id}/{event_id}/missing.docx"
    job = DealJobs(
        organisation_id=org_id,
        user_id=uuid4(),
        job_type=DealJobType.TRANSCRIPT_UPLOAD,
        deal_id=deal_id,
        event_id=event_id,
        status=JobStatus.PENDING,
        job_metadata={"storage_path": storage_path},
    )
    session = _FakeSession(get_result=job)
    service = MeetingsService(
        session,
        org_id,
        storage_factory=_storage_factory({storage_path: None}),
    )

    with pytest.raises(MeetingsServiceError) as exc:
        await service.finalize_transcript_upload(deal_id, event_id, job.job_id)

    assert exc.value.status_code == 400
    assert exc.value.message == "Uploaded transcript file was not found. Please retry the upload."
    assert job.status == JobStatus.FAILED
    assert (
        job.error_message == "Transcript file was not uploaded to storage. Please retry the upload."
    )
    assert session.commits == 1
