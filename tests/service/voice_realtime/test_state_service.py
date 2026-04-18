from __future__ import annotations

from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.job_models import ChatMessageJob, JobStatus
from src.service.voice_realtime import state_service as state_module
from src.service.voice_realtime.state_service import VoiceRealtimeStateService


class FakeChatSession:
    def __init__(self):
        self.properties: dict[str, object] = {}


class FakeResult:
    def __init__(self, chat_session):
        self._chat_session = chat_session

    def scalars(self):
        return self

    def one_or_none(self):
        return self._chat_session


class FakeSession:
    def __init__(self, chat_session, job):
        self.chat_session = chat_session
        self.job = job
        self.added: list[object] = []

    async def execute(self, stmt):
        return FakeResult(self.chat_session)

    def add(self, value) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        return None

    def get(self, cls, obj_id):
        if cls is ChatMessageJob:
            return self.job
        return None


@pytest.mark.asyncio
async def test_update_call_state_marks_voice_job_completed(monkeypatch) -> None:
    chat_session = FakeChatSession()
    job = ChatMessageJob(
        job_type="voice_realtime_sideband",
        chat_type="VOICE_CHAT",
        user_id=uuid4(),
        job_metadata={},
    )
    fake_session = FakeSession(chat_session, job)

    async def fake_acquire_org_async_session(_organisation_id):
        yield fake_session

    monkeypatch.setattr(state_module, "acquire_org_async_session", fake_acquire_org_async_session)

    service = VoiceRealtimeStateService(
        organisation_id=uuid4(),
        user_id=uuid4(),
        chat_session_id=str(uuid4()),
        session_id="azure-session-123",
        call_id="call-123",
        job_id=job.job_id,
    )

    await service.update_call_state(status="completed")

    assert chat_session.properties["voice_sideband_status"] == "completed"
    assert job.status == JobStatus.COMPLETED
    assert job.job_metadata["voice_sideband_status"] == "completed"


@pytest.mark.asyncio
async def test_update_call_state_marks_voice_job_failed_with_error(monkeypatch) -> None:
    chat_session = FakeChatSession()
    job = ChatMessageJob(
        job_type="voice_realtime_sideband",
        chat_type="VOICE_CHAT",
        user_id=uuid4(),
        job_metadata={},
    )
    fake_session = FakeSession(chat_session, job)

    async def fake_acquire_org_async_session(_organisation_id):
        yield fake_session

    monkeypatch.setattr(state_module, "acquire_org_async_session", fake_acquire_org_async_session)

    service = VoiceRealtimeStateService(
        organisation_id=uuid4(),
        user_id=uuid4(),
        chat_session_id=str(uuid4()),
        session_id="azure-session-123",
        call_id="call-123",
        job_id=job.job_id,
    )

    await service.update_call_state(status="failed", error="boom")

    assert chat_session.properties["voice_sideband_status"] == "failed"
    assert chat_session.properties["voice_sideband_error"] == "boom"
    assert job.status == JobStatus.FAILED
    assert job.error_message == "boom"
    assert job.job_metadata["voice_sideband_status"] == "failed"
