# Meeting Recording Playback Download Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users open a meeting recording page, play the mixed meeting video, and download the MP4 from our storage-backed meeting recording metadata.

**Architecture:** Keep the current `/users/meetings/{event_id}/recording` detail endpoint as the single frontend data source. Persist stable video artifact fields on `meeting_recording_sessions`, including a stable non-SAS blob URL; do not persist expiring SAS URLs. Generate short-lived `playback_url` and `download_url` on demand from the stored blob path, and update the existing frontend recording page to show a normal video player plus a download action.

**Tech Stack:** FastAPI/Pydantic, SQLModel, Alembic org migrations, Azure Blob SAS via `AzureOrganisationBlobStorage`, Recall webhook artifact archival, React/Next.js Meetings module, Jest.

---

## Current Evidence

Backend:

- `src/service/meeting_bot/artifacts_service.py` downloads Recall `video_mixed.done` media and stores MP4 bytes under `meeting_recordings/{event_id}/{recording_id}/{video_id}.mp4`.
- `src/service/meeting_bot/webhook_service.py` stores the archived video artifact inside `meeting_recording_sessions.provider_payload["video_artifact"]`.
- `src/service/meetings_tab_service.py` currently returns `MeetingRecordingResponse.playback_url` by reading `provider_payload.video_artifact.archived_blob_path` and generating a 120-minute SAS.
- `src/api/schemas/meetings_tab_models.py` exposes only `playback_url`, not a separate download URL, filename, content type, size, or durable media path.
- `src/domain/models/organisation_tables/sync/meeting_recording.py` has no first-class video metadata columns. Durable video data is currently buried in JSONB provider payload.

Frontend:

- `D:\CopilotGTM\copilot-app-frontend\src\modules\meetings\components\recording-page.tsx` already renders an HTML5 `<video controls src={recording.playback_url} />`.
- That page does not expose a dedicated download button.
- `D:\CopilotGTM\copilot-app-frontend\src\modules\meetings\types\meetings.types.ts` has `playback_url` but no `download_url`, `video_filename`, `video_content_type`, or `video_size_bytes`.
- `D:\CopilotGTM\copilot-app-frontend\__tests__\unit\modules\meetings\recording-page.test.tsx` already mocks `playback_url` and is the right focused frontend test target.

## Design Decisions

1. Store a stable URL, not an expiring SAS URL.
   Store `video_blob_url` as a stable non-SAS Azure blob URL and `video_blob_path` as the operational lookup key. Generate fresh `playback_url` and `download_url` in the API. Azure SAS URLs expire and are bearer secrets, so persisting them creates stale links and avoidable exposure.

2. Keep the current blob path shape.
   `meeting_recordings/{event_id}/{recording_id}/{video_id}.mp4` is good enough: it is org-container scoped, event-grouped, readable for support, and avoids a new storage hierarchy. The API still enforces current-user meeting ownership before minting URLs.

3. Add first-class video columns to `meeting_recording_sessions`.
   JSONB provider payload is useful for raw provider snapshots, but the app needs stable product metadata for playback/download. A few nullable columns are simpler than a new table.

4. Use one endpoint response for play and download.
   No new route is needed for V1. `GET /users/meetings/{event_id}/recording` can return `playback_url` and `download_url` together. The frontend refetches the endpoint when the page opens, so URL freshness is already aligned with the current hook.

5. Use inline playback for normal meeting app behavior.
   The existing recording page should keep the video above summary/transcript, with native controls, and add a visible download button near Share/View record. Do not build a custom media player.

---

## Files

Backend repo: `D:\CopilotGTM\copilot-backend`

- Modify: `src/domain/models/organisation_tables/sync/meeting_recording.py`
  Add nullable stable video artifact columns.
- Create: `migrations/org_migrations/versions/3c7f79637727_072_add_meeting_recording_video_artifact_columns.py`
  Add/drop the new org-table columns idempotently.
- Modify: `src/service/meeting_bot/session_service.py`
  Add a narrow method to persist video artifact columns for the current provider session.
- Modify: `src/service/meeting_bot/artifacts_service.py`
  Return stable non-SAS blob URL with archived video metadata.
- Modify: `src/service/meeting_bot/webhook_service.py`
  After archiving `video_mixed.done`, persist both provider payload and first-class video columns.
- Modify: `src/service/meetings_tab_service.py`
  Resolve artifact metadata from first-class columns first, provider payload fallback second; return play and download URLs.
- Modify: `src/api/schemas/meetings_tab_models.py`
  Extend `MeetingRecordingResponse`.
- Modify: `docs/API Documentation/meetings-tab-api.md`
  Document the new fields and frontend behavior.
- Modify: `tests/service/meeting_bot/test_meeting_recording_session_service.py`
  Test the session service column persistence.
- Modify: `tests/service/meeting_bot/test_meeting_recording_webhook_service.py`
  Test `video_mixed.done` persists video metadata.
- Modify: `tests/service/test_meetings_tab_service.py`
  Test recording detail uses DB columns, generates play/download URLs, and preserves fallback behavior.

Frontend repo: `D:\CopilotGTM\copilot-app-frontend`

- Modify: `src/modules/meetings/types/meetings.types.ts`
  Add new recording response fields.
- Modify: `src/modules/meetings/components/recording-page.tsx`
  Add download button and video metadata display using existing layout.
- Modify: `__tests__/unit/modules/meetings/recording-page.test.tsx`
  Verify video element and download button render.

---

## Backend Tasks

### Task 1: Add Stable Video Artifact Columns

**Files:**

- Modify: `src/domain/models/organisation_tables/sync/meeting_recording.py`
- Create: `migrations/org_migrations/versions/3c7f79637727_072_add_meeting_recording_video_artifact_columns.py`
- Test: `tests/service/meeting_bot/test_meeting_intelligence_model.py`

- [ ] **Step 1: Add a model test for the new nullable columns**

Append this test to `tests/service/meeting_bot/test_meeting_intelligence_model.py`:

```python
from src.domain.models.organisation_tables.sync.meeting_recording import MeetingRecordingSession


def test_meeting_recording_session_has_nullable_video_artifact_columns() -> None:
    columns = MeetingRecordingSession.__table__.c

    assert columns.video_blob_path.nullable is True
    assert columns.video_blob_url.nullable is True
    assert columns.video_content_type.nullable is True
    assert columns.video_size_bytes.nullable is True
    assert columns.video_recording_id.nullable is True
    assert columns.video_artifact_id.nullable is True
    assert columns.video_archived_at.nullable is True
```

- [ ] **Step 2: Run the focused model test and confirm it fails**

Run:

```powershell
uv run pytest tests\service\meeting_bot\test_meeting_intelligence_model.py::test_meeting_recording_session_has_nullable_video_artifact_columns -q --import-mode=importlib
```

Expected: fails because `video_blob_path` does not exist.

- [ ] **Step 3: Add columns to the SQLModel**

In `src/domain/models/organisation_tables/sync/meeting_recording.py`, update the SQLAlchemy import:

```python
from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, String, Text, UniqueConstraint, text
```

Add these fields after `transcript_ready_at`:

```python
    video_blob_path: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Stable org-blob path for the archived mixed MP4.",
    )
    video_blob_url: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Stable non-SAS blob URL for the archived mixed MP4.",
    )
    video_content_type: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
        description="Content type stored with the archived mixed MP4.",
    )
    video_size_bytes: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
        description="Archived mixed MP4 size when known.",
    )
    video_recording_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider recording id for the archived mixed MP4.",
    )
    video_artifact_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider video artifact id for the archived mixed MP4.",
    )
    video_archived_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="When the mixed MP4 was archived into org storage.",
    )
```

- [ ] **Step 4: Add org migration**

Create `migrations/org_migrations/versions/3c7f79637727_072_add_meeting_recording_video_artifact_columns.py`:

```python
"""072 add meeting recording video artifact columns

Revision ID: 3c7f79637727
Revises: 3c7f79637728
Create Date: 2026-07-06 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7f79637727"
down_revision: str | None = "3c7f79637728"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if not _has_column(table_name, column.name):
        op.add_column(table_name, column)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _has_column(table_name, column_name):
        op.drop_column(table_name, column_name)


def upgrade() -> None:
    table_name = "meeting_recording_sessions"
    _add_column_if_missing(table_name, sa.Column("video_blob_path", sa.Text(), nullable=True))
    _add_column_if_missing(table_name, sa.Column("video_blob_url", sa.Text(), nullable=True))
    _add_column_if_missing(table_name, sa.Column("video_content_type", sa.String(length=128), nullable=True))
    _add_column_if_missing(table_name, sa.Column("video_size_bytes", sa.BigInteger(), nullable=True))
    _add_column_if_missing(table_name, sa.Column("video_recording_id", sa.String(length=255), nullable=True))
    _add_column_if_missing(table_name, sa.Column("video_artifact_id", sa.String(length=255), nullable=True))
    _add_column_if_missing(table_name, sa.Column("video_archived_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    table_name = "meeting_recording_sessions"
    _drop_column_if_exists(table_name, "video_archived_at")
    _drop_column_if_exists(table_name, "video_artifact_id")
    _drop_column_if_exists(table_name, "video_recording_id")
    _drop_column_if_exists(table_name, "video_size_bytes")
    _drop_column_if_exists(table_name, "video_content_type")
    _drop_column_if_exists(table_name, "video_blob_url")
    _drop_column_if_exists(table_name, "video_blob_path")
```

- [ ] **Step 5: Run focused verification**

Run:

```powershell
uv run pytest tests\service\meeting_bot\test_meeting_intelligence_model.py -q --import-mode=importlib
uv run python -m py_compile src\domain\models\organisation_tables\sync\meeting_recording.py migrations\org_migrations\versions\3c7f79637727_072_add_meeting_recording_video_artifact_columns.py
```

Expected: tests pass and py_compile prints no output.

### Task 2: Persist Video Metadata From Webhook Archival

**Files:**

- Modify: `src/service/meeting_bot/artifacts_service.py`
- Modify: `src/service/meeting_bot/session_service.py`
- Modify: `src/service/meeting_bot/webhook_service.py`
- Test: `tests/service/meeting_bot/test_meeting_recording_webhook_service.py`
- Test: `tests/service/meeting_bot/test_meeting_recording_session_service.py`

- [ ] **Step 1: Extend artifact archival test to require stable blob URL**

In `tests/service/meeting_bot/test_meeting_recording_webhook_service.py`, update `_FakeStorage` inside `test_archive_video_artifact_downloads_to_org_blob`:

```python
    class _FakeStorage:
        def __init__(self, storage_org_id):
            assert storage_org_id == organisation_id
            self.account_name = "storageacct"
            self.container = "org-container"
```

Add this assertion after `assert result["archived_blob_path"] == ...`:

```python
    assert (
        result["archived_blob_url"]
        == f"https://storageacct.blob.core.windows.net/org-container/meeting_recordings/{event_id}/rec_1/video_1.mp4"
    )
```

- [ ] **Step 2: Run the artifact test and confirm it fails**

Run:

```powershell
uv run pytest tests\service\meeting_bot\test_meeting_recording_webhook_service.py::test_archive_video_artifact_downloads_to_org_blob -q --import-mode=importlib
```

Expected: fails because `archived_blob_url` is not returned.

- [ ] **Step 3: Return stable blob URL from artifact archival**

In `src/service/meeting_bot/artifacts_service.py`, after `blob_path = ...`, add:

```python
        blob_url = (
            f"https://{storage.account_name}.blob.core.windows.net/"
            f"{storage.container}/{blob_path}"
        )
```

Then include it in the returned `result`:

```python
                    "archived_blob_url": blob_url,
```

The resulting block should include:

```python
                result: dict[str, Any] = {
                    **video_artifact,
                    "archived_blob_path": blob_path,
                    "archived_blob_url": blob_url,
                    "archived_content_type": content_type,
                    "archived_at": datetime.now(UTC).isoformat(),
                }
```

- [ ] **Step 4: Add session-service test**

Append to `tests/service/meeting_bot/test_meeting_recording_session_service.py`:

```python
@pytest.mark.asyncio
async def test_mark_video_artifact_archived_updates_stable_columns(monkeypatch) -> None:
    organisation_id = uuid4()
    session_id = uuid4()
    event_id = uuid4()
    record = MeetingRecordingSession(
        session_id=session_id,
        organisation_id=organisation_id,
        calendar_event_id=event_id,
        provider=MeetingRecordingProvider.RECALL_AI,
        dedupe_key="key",
        meeting_url="https://meet.example/join",
        join_at=datetime.now(UTC),
        status=MeetingRecordingStatus.COMPLETED,
        transcript_status=MeetingTranscriptStatus.READY,
        provider_session_id="bot_123",
        provider_payload={"id": "bot_123"},
    )

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, model, key):
            assert model is MeetingRecordingSession
            assert key == session_id
            return record

        async def commit(self):
            return None

        async def refresh(self, value):
            assert value is record

    service = MeetingRecordingSessionService(organisation_id)
    monkeypatch.setattr(
        "src.service.meeting_bot.session_service.acquire_org_async_session",
        lambda _org_id: _Session(),
    )

    result = await service.mark_video_artifact_archived(
        session_id,
        {
            "recording_id": "rec_1",
            "video_id": "video_1",
            "archived_blob_path": "meeting_recordings/event/rec_1/video_1.mp4",
            "archived_blob_url": "https://storage.example/container/meeting_recordings/event/rec_1/video_1.mp4",
            "archived_content_type": "video/mp4",
            "archived_size_bytes": 7,
            "archived_at": "2026-07-06T01:00:00+00:00",
        },
    )

    assert result is record
    assert record.video_blob_path == "meeting_recordings/event/rec_1/video_1.mp4"
    assert record.video_blob_url == "https://storage.example/container/meeting_recordings/event/rec_1/video_1.mp4"
    assert record.video_content_type == "video/mp4"
    assert record.video_size_bytes == 7
    assert record.video_recording_id == "rec_1"
    assert record.video_artifact_id == "video_1"
    assert record.video_archived_at == datetime(2026, 7, 6, 1, 0, tzinfo=UTC)
```

- [ ] **Step 5: Run the session-service test and confirm it fails**

Run:

```powershell
uv run pytest tests\service\meeting_bot\test_meeting_recording_session_service.py::test_mark_video_artifact_archived_updates_stable_columns -q --import-mode=importlib
```

Expected: fails because `mark_video_artifact_archived` does not exist.

- [ ] **Step 6: Implement the session-service method**

In `src/service/meeting_bot/session_service.py`, add imports if missing:

```python
from datetime import UTC, datetime
```

Add this method to `MeetingRecordingSessionService`:

```python
    async def mark_video_artifact_archived(
        self,
        session_id: UUID,
        video_artifact: dict[str, Any],
    ) -> MeetingRecordingSession | None:
        """Persist stable mixed-video metadata outside provider JSON."""
        blob_path = video_artifact.get("archived_blob_path")
        if not isinstance(blob_path, str) or not blob_path:
            return None

        async with acquire_org_async_session(self.organisation_id) as session:
            record = await session.get(MeetingRecordingSession, session_id)
            if record is None:
                return None
            record.video_blob_path = blob_path
            record.video_blob_url = _string_or_none(video_artifact.get("archived_blob_url"))
            record.video_content_type = _string_or_none(video_artifact.get("archived_content_type"))
            record.video_size_bytes = _int_or_none(video_artifact.get("archived_size_bytes"))
            record.video_recording_id = _string_or_none(video_artifact.get("recording_id"))
            record.video_artifact_id = _string_or_none(video_artifact.get("video_id"))
            record.video_archived_at = _parse_iso_datetime(video_artifact.get("archived_at"))
            await session.commit()
            await session.refresh(record)
            return record
```

Add helpers at module level:

```python
def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
```

- [ ] **Step 7: Update webhook service to persist columns after archival**

In `src/service/meeting_bot/webhook_service.py`, inside the `if video_artifact is not None:` block, immediately after:

```python
                merged_payload["video_artifact"] = video_artifact
```

add:

```python
                await self.session_service.mark_video_artifact_archived(
                    existing.session_id,
                    video_artifact,
                )
```

This is intentionally after archival and before `apply_webhook_update`; if the metadata write fails, the webhook should fail and be retried rather than silently losing durable media metadata.

- [ ] **Step 8: Update webhook test expectation**

In `tests/service/meeting_bot/test_meeting_recording_webhook_service.py`, in `_make_service`, add the mock method:

```python
        mark_video_artifact_archived=AsyncMock(),
```

In `test_video_mixed_done_fetches_video_artifact`, after the existing `archive_mock.assert_awaited_once_with(...)`, add:

```python
    service.session_service.mark_video_artifact_archived.assert_awaited_once_with(
        existing.session_id,
        {
            "recording_id": "rec_1",
            "video_id": "video_1",
            "download_url": "https://download.example/video.mp4",
            "archived_blob_path": "meeting_recordings/event/rec_1/video_1.mp4",
            "archived_blob_url": "https://storage.example/container/meeting_recordings/event/rec_1/video_1.mp4",
        },
    )
```

- [ ] **Step 9: Run focused backend tests**

Run:

```powershell
uv run pytest tests\service\meeting_bot\test_meeting_recording_webhook_service.py::test_archive_video_artifact_downloads_to_org_blob tests\service\meeting_bot\test_meeting_recording_session_service.py::test_mark_video_artifact_archived_updates_stable_columns tests\service\meeting_bot\test_meeting_recording_webhook_service.py::test_video_mixed_done_fetches_video_artifact -q --import-mode=importlib
```

Expected: both pass.

### Task 3: Extend Recording Detail API For Play And Download

**Files:**

- Modify: `src/api/schemas/meetings_tab_models.py`
- Modify: `src/service/meetings_tab_service.py`
- Modify: `docs/API Documentation/meetings-tab-api.md`
- Test: `tests/service/test_meetings_tab_service.py`

- [ ] **Step 1: Add failing tests for DB-column playback and download URLs**

Append to `tests/service/test_meetings_tab_service.py`:

```python
@pytest.mark.asyncio
async def test_recording_detail_uses_video_columns_for_playback_and_download(monkeypatch) -> None:
    service = _service()

    async def _rollback() -> None:
        return None

    service.session.rollback = _rollback
    event = SimpleNamespace(event_id=uuid4(), title="Discovery call")
    recording = SimpleNamespace(
        status="COMPLETED",
        transcript_status="READY",
        provider_session_id=None,
        provider_payload={},
        video_blob_path="meeting_recordings/event/rec_1/video_123.mp4",
        video_blob_url="https://storage.example/container/meeting_recordings/event/rec_1/video_123.mp4",
        video_content_type="video/mp4",
        video_size_bytes=123456,
        video_recording_id="rec_1",
        video_artifact_id="video_123",
        video_archived_at=datetime(2026, 7, 6, 1, 0, tzinfo=UTC),
    )

    class _FakeStorage:
        def __init__(self, organisation_id):
            assert organisation_id == service.organisation_id

        async def generate_read_sas(self, blob_path, *, minutes, download_filename=None):
            assert blob_path == "meeting_recordings/event/rec_1/video_123.mp4"
            if download_filename:
                return "https://blob.example/download.mp4?sas=1"
            return "https://blob.example/play.mp4?sas=1"

        async def close(self):
            return None

    async def _get_row(_event_id):
        return (event, None, None, recording, None)

    monkeypatch.setattr(service, "_get_meeting_row", _get_row)
    monkeypatch.setattr("src.service.meetings_tab_service.AzureOrganisationBlobStorage", _FakeStorage)

    detail = await service.get_recording(event.event_id)

    assert detail.playback_url == "https://blob.example/play.mp4?sas=1"
    assert detail.download_url == "https://blob.example/download.mp4?sas=1"
    assert detail.video_blob_path == "meeting_recordings/event/rec_1/video_123.mp4"
    assert detail.video_blob_url == "https://storage.example/container/meeting_recordings/event/rec_1/video_123.mp4"
    assert detail.video_content_type == "video/mp4"
    assert detail.video_size_bytes == 123456
    assert detail.video_filename == "Discovery call.mp4"
```

- [ ] **Step 2: Run the test and confirm it fails**

Run:

```powershell
uv run pytest tests\service\test_meetings_tab_service.py::test_recording_detail_uses_video_columns_for_playback_and_download -q --import-mode=importlib
```

Expected: fails because response fields do not exist.

- [ ] **Step 3: Extend the response schema**

In `src/api/schemas/meetings_tab_models.py`, update `MeetingRecordingResponse`:

```python
class MeetingRecordingResponse(BaseModel):
    """Recall-bot recording detail for the recording intelligence page."""

    event_id: UUID
    recording_status: str | None = None
    transcript_status: str | None = None
    playback_url: str | None = None
    download_url: str | None = None
    video_blob_path: str | None = None
    video_blob_url: str | None = None
    video_content_type: str | None = None
    video_size_bytes: int | None = None
    video_filename: str | None = None
    provider_session_id: str | None = None
    transcript_artifact: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    action_items: list[dict[str, Any]] = Field(default_factory=list)
    transcript_segments: list[MeetingTranscriptSegment] = Field(default_factory=list)
```

- [ ] **Step 4: Add artifact resolution helpers**

In `src/service/meetings_tab_service.py`, replace `_recording_playback_url` with a method that returns both URLs:

```python
    async def _recording_video_urls(
        self,
        playback_artifact: dict[str, Any],
        *,
        download_filename: str,
    ) -> tuple[str | None, str | None]:
        archived_blob_path = playback_artifact.get("archived_blob_path")
        if isinstance(archived_blob_path, str) and archived_blob_path:
            storage = AzureOrganisationBlobStorage(self.organisation_id)
            try:
                playback_url = await storage.generate_read_sas(archived_blob_path, minutes=120)
                download_url = await storage.generate_read_sas(
                    archived_blob_path,
                    minutes=120,
                    download_filename=download_filename,
                )
                return playback_url, download_url
            finally:
                await storage.close()
        download_url = playback_artifact.get("download_url")
        return (download_url if isinstance(download_url, str) else None, None)
```

Add helpers near other static helpers:

```python
    @staticmethod
    def _video_artifact_from_recording(recording: MeetingRecordingSession) -> dict[str, Any] | None:
        if recording.video_blob_path:
            return {
                "archived_blob_path": recording.video_blob_path,
                "archived_blob_url": recording.video_blob_url,
                "archived_content_type": recording.video_content_type,
                "archived_size_bytes": recording.video_size_bytes,
                "recording_id": recording.video_recording_id,
                "video_id": recording.video_artifact_id,
            }
        provider_payload = dict(recording.provider_payload or {})
        artifact = provider_payload.get("video_artifact")
        return artifact if isinstance(artifact, dict) else None

    @staticmethod
    def _recording_download_filename(event: CalendarEvent) -> str:
        title = (event.title or "meeting-recording").strip()
        cleaned = "".join(char if char.isalnum() or char in {" ", "-", "_"} else "_" for char in title)
        cleaned = "_".join(cleaned.split())
        return f"{cleaned or 'meeting-recording'}.mp4"
```

- [ ] **Step 5: Use the helper in `get_recording`**

In `get_recording`, replace the existing `playback_artifact` and `playback_url` section with:

```python
        playback_artifact = self._video_artifact_from_recording(recording) or (
            RecallMeetingRecordingProvider.extract_video_mixed_download(bot_snapshot)
            if isinstance(bot_snapshot, dict)
            else None
        )
        if not playback_artifact:
            raise MeetingsTabServiceError("Recording is not ready", 409, code="recording_not_ready")
        video_filename = self._recording_download_filename(event)
        playback_url, download_url = await self._recording_video_urls(
            playback_artifact,
            download_filename=video_filename,
        )
```

Update the `MeetingRecordingResponse(...)` construction:

```python
            playback_url=playback_url,
            download_url=download_url,
            video_blob_path=playback_artifact.get("archived_blob_path"),
            video_blob_url=playback_artifact.get("archived_blob_url"),
            video_content_type=playback_artifact.get("archived_content_type"),
            video_size_bytes=playback_artifact.get("archived_size_bytes"),
            video_filename=video_filename,
```

- [ ] **Step 6: Update API docs**

In `docs/API Documentation/meetings-tab-api.md`, update `MeetingRecordingResponse`:

```ts
type MeetingRecordingResponse = {
  event_id: string;
  recording_status: string | null;
  transcript_status: string | null;
  playback_url: string | null;
  download_url: string | null;
  video_blob_path: string | null;
  video_blob_url: string | null;
  video_content_type: string | null;
  video_size_bytes: number | null;
  video_filename: string | null;
  provider_session_id: string | null;
  transcript_artifact: Record<string, unknown> | null;
  summary: Record<string, unknown> | null;
  action_items: Array<Record<string, unknown>>;
  transcript_segments: Array<{
    start_time: number | null;
    speaker_id: string | null;
    speaker_name: string | null;
    text: string;
  }>;
};
```

Add these frontend notes:

```markdown
- Use `playback_url` as the `<video src>` for inline playback.
- Use `download_url` for the Download button. It may differ from `playback_url` because it includes download content-disposition.
- Do not persist either URL client-side; refetch this endpoint when opening the page because both URLs are short-lived.
- `video_blob_path` and `video_blob_url` are returned for debugging/support only. Do not expose them as clickable URLs.
```

- [ ] **Step 7: Run focused backend tests**

Run:

```powershell
uv run pytest tests\service\test_meetings_tab_service.py::test_recording_detail_uses_video_columns_for_playback_and_download tests\service\test_meetings_tab_service.py::test_recording_detail_uses_archived_video_sas tests\service\test_meetings_tab_service.py::test_recording_detail_uses_persisted_video_artifact -q --import-mode=importlib
```

Expected: all pass.

---

## Frontend Tasks

### Task 4: Add Download Fields To Frontend Types

**Files:**

- Modify: `D:\CopilotGTM\copilot-app-frontend\src\modules\meetings\types\meetings.types.ts`

- [ ] **Step 1: Extend `MeetingRecordingResponse`**

Update the interface:

```ts
export interface MeetingRecordingResponse {
  event_id: string;
  recording_status: string | null;
  transcript_status: string | null;
  playback_url: string | null;
  download_url: string | null;
  video_blob_path: string | null;
  video_blob_url: string | null;
  video_content_type: string | null;
  video_size_bytes: number | null;
  video_filename: string | null;
  provider_session_id: string | null;
  transcript_artifact: string | Record<string, unknown> | null;
  summary: string | string[] | Record<string, unknown> | null;
  action_items: MeetingActionItem[] | null;
  transcript_segments: MeetingTranscriptSegment[];
}
```

- [ ] **Step 2: Run frontend type check after Task 5**

Run after the UI change:

```powershell
yarn check-types
```

Expected: type check passes.

### Task 5: Add Download Action To Existing Recording Page

**Files:**

- Modify: `D:\CopilotGTM\copilot-app-frontend\src\modules\meetings\components\recording-page.tsx`
- Test: `D:\CopilotGTM\copilot-app-frontend\__tests__\unit\modules\meetings\recording-page.test.tsx`

- [ ] **Step 1: Add failing frontend test**

In `recording-page.test.tsx`, update the recording mock:

```ts
download_url: "https://example.com/video-download.mp4",
video_blob_path: "meeting_recordings/event/rec_1/video_1.mp4",
video_blob_url: "https://storage.example/container/meeting_recordings/event/rec_1/video_1.mp4",
video_content_type: "video/mp4",
video_size_bytes: 123456,
video_filename: "Discovery_call.mp4",
```

Add this test:

```tsx
it("renders playable video and download action when recording media is available", () => {
  render(<MeetingRecordingPage eventId="event-1" />);

  const video = document.querySelector("video");
  expect(video).toHaveAttribute("src", "https://example.com/video.mp4");

  const download = screen.getByRole("link", { name: /download/i });
  expect(download).toHaveAttribute("href", "https://example.com/video-download.mp4");
  expect(download).toHaveAttribute("download", "Discovery_call.mp4");
});
```

- [ ] **Step 2: Run the focused frontend test and confirm it fails**

Run from `D:\CopilotGTM\copilot-app-frontend`:

```powershell
yarn test:jest __tests__/unit/modules/meetings/recording-page.test.tsx --runInBand
```

Expected: fails because no Download link exists.

- [ ] **Step 3: Add the download button**

In `recording-page.tsx`, import the icon:

```tsx
import { Download } from "lucide-react";
```

Near the existing `Share` and `View record` buttons, add:

```tsx
          {recording?.download_url ? (
            <Button variant="outline" asChild>
              <a
                href={recording.download_url}
                download={recording.video_filename ?? "meeting-recording.mp4"}
              >
                <Download className="mr-2 size-4" />
                Download
              </a>
            </Button>
          ) : null}
```

Keep this as a normal anchor so browser download behavior works without custom blob-fetch code.

- [ ] **Step 4: Add small metadata under the video**

Below the video container, add:

```tsx
          {recording?.video_size_bytes || recording?.video_content_type ? (
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              {recording.video_content_type ? <span>{recording.video_content_type}</span> : null}
              {recording.video_size_bytes ? (
                <span>{formatFileSize(recording.video_size_bytes)}</span>
              ) : null}
            </div>
          ) : null}
```

Add helper in the same file:

```tsx
function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(1)} KB`;
  const mb = kb / 1024;
  if (mb < 1024) return `${mb.toFixed(1)} MB`;
  return `${(mb / 1024).toFixed(1)} GB`;
}
```

- [ ] **Step 5: Run focused frontend tests**

Run from `D:\CopilotGTM\copilot-app-frontend`:

```powershell
yarn test:jest __tests__/unit/modules/meetings/recording-page.test.tsx --runInBand
yarn check-types
```

Expected: both pass.

---

## Final Verification

Backend:

```powershell
uv run pytest tests\service\meeting_bot\test_meeting_intelligence_model.py tests\service\meeting_bot\test_meeting_recording_session_service.py tests\service\meeting_bot\test_meeting_recording_webhook_service.py tests\service\test_meetings_tab_service.py -q --import-mode=importlib
uv run ruff check src\domain\models\organisation_tables\sync\meeting_recording.py src\service\meeting_bot\artifacts_service.py src\service\meeting_bot\session_service.py src\service\meeting_bot\webhook_service.py src\service\meetings_tab_service.py src\api\schemas\meetings_tab_models.py tests\service\meeting_bot\test_meeting_recording_session_service.py tests\service\meeting_bot\test_meeting_recording_webhook_service.py tests\service\test_meetings_tab_service.py
uv run python -m py_compile src\domain\models\organisation_tables\sync\meeting_recording.py src\service\meeting_bot\artifacts_service.py src\service\meeting_bot\session_service.py src\service\meeting_bot\webhook_service.py src\service\meetings_tab_service.py src\api\schemas\meetings_tab_models.py
```

Frontend:

```powershell
yarn test:jest __tests__/unit/modules/meetings/recording-page.test.tsx --runInBand
yarn check-types
```

Manual dev validation after deploy:

1. Trigger or replay a dev `video_mixed.done` webhook for a completed Recall bot.
2. Verify `meeting_recording_sessions.video_blob_path` is populated with `meeting_recordings/{event_id}/{recording_id}/{video_id}.mp4`.
3. Open `/meetings/recordings/{event_id}`.
4. Confirm the video plays inline.
5. Click Download and confirm the browser downloads an MP4 using `video_filename`.
6. Refresh the page after the SAS expires and confirm the API returns fresh URLs.

## Completion Criteria

- Stable video metadata and stable non-SAS blob URL are stored in database columns, not only `provider_payload`.
- No expiring SAS URL is stored in the database.
- The existing archived blob path remains org-scoped and event-scoped.
- `/users/meetings/{event_id}/recording` returns both `playback_url` and `download_url`.
- Frontend recording page shows a playable video and a Download button.
- Focused backend and frontend tests pass.
- API docs describe play/download URL semantics and warn that URLs are short-lived.
