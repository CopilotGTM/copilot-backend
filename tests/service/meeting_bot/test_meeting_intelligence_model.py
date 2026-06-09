from sqlalchemy import Text
from src.domain.models.organisation_tables.sync.meeting_intelligence import Meeting


def test_meeting_has_nullable_compressed_transcript_cache_columns():
    columns = Meeting.__table__.columns

    assert "compressed_transcript" in columns
    assert isinstance(columns["compressed_transcript"].type, Text)
    assert columns["compressed_transcript"].nullable is True
    assert "compressed_transcript_generated_at" in columns
    assert columns["compressed_transcript_generated_at"].nullable is True
    assert "compressed_transcript_source_hash" in columns
    assert columns["compressed_transcript_source_hash"].nullable is True
