"""Service for processing manually uploaded transcript files.

Extracts text from PDF/Word files and saves as a Meeting record,
then triggers meeting summary generation.

Session Handling:
- Acquires session only when needed for DB operations
- Releases immediately after each operation
- Never holds session during file processing
"""

from __future__ import annotations

import io
import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import (
    DealJobs,
    DealJobType,
    JobStatus,
)
from src.domain.models.organisation_tables.sync.meeting_intelligence import (
    Meeting,
    MeetingSource,
    MeetingStatus,
    TranscriptSegment,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher

logger = get_logger(__name__)


class TranscriptUploadError(Exception):
    """Non-retryable error during transcript upload processing."""

    def __init__(self, message: str, error_code: str = "transcript_upload_error"):
        self.message = message
        self.error_code = error_code
        super().__init__(message)


class TranscriptUploadService:
    """Extracts text from uploaded transcript files and creates Meeting records.

    Follows fresh session pattern — acquires DB sessions on-demand and releases
    before any long-running operations.
    """

    def __init__(
        self,
        organisation_id: UUID,
        deal_id: UUID,
        event_id: UUID,
        user_id: UUID,
        publisher: DealJobRedisPublisher,
    ):
        self.organisation_id = organisation_id
        self.deal_id = deal_id
        self.event_id = event_id
        self.user_id = user_id
        self.publisher = publisher

    async def process(self, storage_path: str, mimetype: str) -> dict:
        """Extract text from uploaded file, save Meeting, trigger summary.

        Args:
            storage_path: Blob storage path for the uploaded file
            mimetype: File MIME type (application/pdf or docx type)

        Returns:
            Result dict with meeting_id and summary_job_id
        """
        self.publisher.publish_progress("Extracting text from uploaded file", "extracting", 0.1)

        # Step 1: Download file from blob storage (no DB session needed)
        storage = AzureOrganisationBlobStorage(self.organisation_id)
        try:
            file_bytes = await storage.get_blob_content(storage_path)
        finally:
            await storage.close()

        # Step 2: Extract text based on file type
        self.publisher.publish_progress("Processing transcript content", "extracting", 0.3)
        transcript_text = self._extract_text(file_bytes, mimetype)

        if not transcript_text or not transcript_text.strip():
            raise TranscriptUploadError(
                "Could not extract any text from the uploaded file. "
                "Please ensure the file contains readable text (not a scanned image).",
                "empty_transcript",
            )

        logger.info(
            "Extracted %d characters from uploaded transcript for event %s",
            len(transcript_text),
            self.event_id,
        )

        # Step 3: Save Meeting record (fresh session)
        self.publisher.publish_progress("Saving transcript", "saving", 0.6)
        meeting_id = await self._save_meeting(transcript_text, storage_path)

        # Step 4: Trigger meeting summary (fresh session)
        self.publisher.publish_progress("Triggering meeting summary", "triggering_summary", 0.8)
        summary_job_id = await self._trigger_meeting_summary()

        return {
            "meeting_id": str(meeting_id),
            "summary_job_id": str(summary_job_id),
            "transcript_length": len(transcript_text),
        }

    def _extract_text(self, file_bytes: bytes, mimetype: str) -> str:
        """Extract plain text from PDF or Word file."""
        if mimetype == "application/pdf":
            return self._extract_from_pdf(file_bytes)
        elif mimetype in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/msword",
        ):
            return self._extract_from_docx(file_bytes)
        else:
            raise TranscriptUploadError(f"Unsupported file type: {mimetype}", "unsupported_format")

    def _extract_from_pdf(self, file_bytes: bytes) -> str:
        """Extract text from PDF using pypdf."""
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(file_bytes))
        pages = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
        return "\n\n".join(pages)

    def _extract_from_docx(self, file_bytes: bytes) -> str:
        """Extract text from Word document using python-docx."""
        from docx import Document

        doc = Document(io.BytesIO(file_bytes))
        paragraphs = []
        for para in doc.paragraphs:
            if para.text.strip():
                paragraphs.append(para.text)
        return "\n\n".join(paragraphs)

    # Pattern: @MM:SS or @H:MM:SS followed by " - Speaker Name"
    _SPEAKER_RE = re.compile(r"^@(\d{1,2}:\d{2}(?::\d{2})?)\s*-\s*(.+)$")

    def _parse_timestamp(self, ts: str) -> float:
        """Convert MM:SS or H:MM:SS to seconds."""
        parts = ts.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])

    def _parse_segments(self, transcript_text: str) -> list[dict]:
        """Parse transcript text into speaker segments.

        Recognises the @MM:SS - Speaker Name pattern commonly found in
        meeting transcripts exported from Otter.ai, Fireflies, etc.
        """
        segments: list[dict] = []
        current_speaker: str | None = None
        current_time: float | None = None
        current_lines: list[str] = []

        for line in transcript_text.split("\n"):
            line = line.strip()
            if not line:
                continue

            match = self._SPEAKER_RE.match(line)
            if match:
                # Flush previous segment
                if current_speaker and current_lines:
                    segments.append(
                        {
                            "speaker_name": current_speaker,
                            "start_time_seconds": current_time,
                            "content": " ".join(current_lines),
                        }
                    )
                current_time = self._parse_timestamp(match.group(1))
                current_speaker = match.group(2).strip()
                current_lines = []
            else:
                current_lines.append(line)

        # Flush last segment
        if current_speaker and current_lines:
            segments.append(
                {
                    "speaker_name": current_speaker,
                    "start_time_seconds": current_time,
                    "content": " ".join(current_lines),
                }
            )

        return segments

    async def _save_meeting(self, transcript_text: str, storage_path: str) -> UUID:
        """Create or update Meeting record with extracted transcript and segments."""
        segments = self._parse_segments(transcript_text)

        async for session in acquire_org_async_session(self.organisation_id):
            # Check if a meeting already exists for this event
            stmt = select(Meeting).where(
                col(Meeting.calendar_event_id) == self.event_id,
                col(Meeting.organisation_id) == self.organisation_id,
            )
            existing = (await session.execute(stmt)).scalars().first()

            if existing and str(existing.status) == MeetingStatus.READY.value:
                raise TranscriptUploadError(
                    "A transcript already exists for this meeting.",
                    "transcript_exists",
                )

            if existing:
                existing.source = MeetingSource.MANUAL_UPLOAD
                existing.status = MeetingStatus.READY
                existing.full_transcript = transcript_text
                existing.transcript_fetched_at = datetime.now(UTC)
                existing.source_data = {"storage_path": storage_path, "upload_type": "manual"}
                await session.flush()
                meeting_id = existing.meeting_id
            else:
                meeting = Meeting(
                    organisation_id=self.organisation_id,
                    calendar_event_id=self.event_id,
                    source=MeetingSource.MANUAL_UPLOAD,
                    status=MeetingStatus.READY,
                    full_transcript=transcript_text,
                    transcript_fetched_at=datetime.now(UTC),
                    source_data={"storage_path": storage_path, "upload_type": "manual"},
                )
                session.add(meeting)
                await session.flush()
                await session.refresh(meeting)
                meeting_id = meeting.meeting_id

            # Save transcript segments
            for idx, seg in enumerate(segments):
                session.add(
                    TranscriptSegment(
                        meeting_id=meeting_id,
                        segment_index=idx,
                        speaker_name=seg["speaker_name"],
                        start_time_seconds=seg["start_time_seconds"],
                        content=seg["content"],
                    )
                )

            await session.commit()

            logger.info(
                "Saved meeting %s with %d transcript segments",
                meeting_id,
                len(segments),
            )
            return meeting_id

        raise RuntimeError("Failed to acquire session")

    async def _trigger_meeting_summary(self) -> UUID:
        """Create a MEETING_SUMMARY job and enqueue it."""
        from src.workers.deal_worker.client import send_deal_task

        async for session in acquire_org_async_session(self.organisation_id):
            job = DealJobs(
                organisation_id=self.organisation_id,
                user_id=self.user_id,
                job_type=DealJobType.MEETING_SUMMARY,
                deal_id=self.deal_id,
                event_id=self.event_id,
                status=JobStatus.PENDING,
                job_metadata={
                    "source": "transcript_upload",
                    "event_id": str(self.event_id),
                },
            )
            session.add(job)
            await session.commit()
            await session.refresh(job)

            send_deal_task(
                "meeting_summary_task.process",
                args=[str(self.organisation_id), str(job.job_id)],
                queue="deal_jobs",
                routing_key="deal_jobs",
            )

            logger.info(
                "Triggered meeting summary job %s after transcript upload for event %s",
                job.job_id,
                self.event_id,
            )
            return job.job_id

        raise RuntimeError("Failed to acquire session")
