from sqlalchemy import Text
from src.domain.models.organisation_tables.sync.meeting_intelligence import Meeting
from src.domain.models.organisation_tables.sync.meeting_recording import MeetingRecordingSession


def test_meeting_has_nullable_compressed_transcript_cache_columns():
    columns = Meeting.__table__.columns

    assert "compressed_transcript" in columns
    assert isinstance(columns["compressed_transcript"].type, Text)
    assert columns["compressed_transcript"].nullable is True
    assert "compressed_transcript_generated_at" in columns
    assert columns["compressed_transcript_generated_at"].nullable is True
    assert "compressed_transcript_source_hash" in columns
    assert columns["compressed_transcript_source_hash"].nullable is True


def test_meeting_recording_session_has_nullable_video_artifact_columns() -> None:
    columns = MeetingRecordingSession.__table__.c

    assert columns.video_blob_path.nullable is True
    assert columns.video_blob_url.nullable is True
    assert columns.video_content_type.nullable is True
    assert columns.video_size_bytes.nullable is True
    assert columns.video_recording_id.nullable is True
    assert columns.video_artifact_id.nullable is True
    assert columns.video_archived_at.nullable is True
