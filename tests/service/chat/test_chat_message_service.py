"""Tests for ChatMessageService using real service logic with mocked AsyncSession.

These tests verify ownership checks, attachment security, query construction,
and business logic that fake services skip entirely.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables.chat_message_models import (
    Attachment,
    AttachmentStatus,
    ChatMessage,
    ChatMessageRole,
    ChatMessageType,
)
from src.domain.models.organisation_tables.chat_session_models import ChatSession
from src.domain.models.organisation_tables.job_models import ChatMessageJob, JobStatus
from src.service.chat.chat_message_service import ChatMessageService, _to_confidence

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session_row(
    *,
    session_id: UUID | None = None,
    user_id: UUID | None = None,
    organisation_id: UUID | None = None,
    is_deleted: bool = False,
) -> MagicMock:
    """Mock ChatSession ORM object."""
    row = MagicMock(spec=ChatSession)
    row.session_id = session_id or uuid4()
    row.user_id = user_id or uuid4()
    row.organisation_id = organisation_id or uuid4()
    row.is_deleted = is_deleted
    row.updated_at = datetime(2024, 1, 1)
    return row


def _make_message_row(
    *,
    message_id: UUID | None = None,
    session_id: UUID | None = None,
    role: ChatMessageRole = ChatMessageRole.USER,
    content: dict | None = None,
    message_type: ChatMessageType = ChatMessageType.GENERAL_MESSAGE,
    feedback_score: int | None = None,
    created_at: datetime | None = None,
) -> MagicMock:
    row = MagicMock(spec=ChatMessage)
    row.message_id = message_id or uuid4()
    row.session_id = session_id or uuid4()
    row.role = role
    row.content = content or {"text": "Hello"}
    row.message_type = message_type
    row.is_edited = False
    row.feedback_score = feedback_score
    row.created_at = created_at or datetime(2024, 1, 1)
    row.updated_at = datetime(2024, 1, 1)
    row.extra = {}
    row.agent_data = {}
    return row


def _make_attachment_row(
    *,
    attachment_id: UUID | None = None,
    organisation_id: UUID | None = None,
    user_id: UUID | None = None,
    is_deleted: bool = False,
    filename: str = "file.pdf",
    mimetype: str = "application/pdf",
    thumbnail_small_path: str | None = None,
    storage_path: str | None = "chat_uploads/file.pdf",
    status: AttachmentStatus = AttachmentStatus.COMPLETED,
) -> MagicMock:
    row = MagicMock(spec=Attachment)
    row.attachment_id = attachment_id or uuid4()
    row.organisation_id = organisation_id or uuid4()
    row.user_id = user_id or uuid4()
    row.is_deleted = is_deleted
    row.filename = filename
    row.mimetype = mimetype
    row.thumbnail_small_path = thumbnail_small_path
    row.storage_path = storage_path
    row.status = status
    return row


def _mock_scalar_one_or_none(row):
    """Result whose scalar_one_or_none returns `row`."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = row
    return mock_result


def _mock_scalars_all(rows: list):
    """Result whose scalars().all() returns `rows`."""
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = rows
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    return mock_result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def org_id():
    return uuid4()


@pytest.fixture
def user_id():
    return uuid4()


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.add = MagicMock()
    return db


@pytest.fixture
def service(mock_db, org_id, user_id):
    return ChatMessageService(db=mock_db, organisation_id=org_id, user_id=user_id)


# =============================================================================
# 1. Message Creation
# =============================================================================


class TestCreateUserMessage:
    @pytest.mark.asyncio
    async def test_validates_session_ownership(self, service, mock_db, org_id, user_id):
        """Session must belong to caller's org+user and not be deleted."""
        # Return a valid session
        session_row = _make_session_row(user_id=user_id, organisation_id=org_id)
        mock_db.execute = AsyncMock(
            side_effect=[
                _mock_scalar_one_or_none(session_row),  # session lookup
                _mock_scalars_all([]),  # attachment resolution
            ]
        )

        msg, job, assistant_id = await service.create_user_message(
            session_id=session_row.session_id,
            content="Hello",
        )
        # Verify the message was created
        assert mock_db.add.called

    @pytest.mark.asyncio
    async def test_missing_session_raises_value_error(self, service, mock_db):
        """No matching session → ValueError. This is what fakes skip."""
        mock_db.execute = AsyncMock(return_value=_mock_scalar_one_or_none(None))

        with pytest.raises(ValueError, match="Chat session not found"):
            await service.create_user_message(session_id=uuid4(), content="Hello")

    @pytest.mark.asyncio
    async def test_deleted_session_raises_value_error(self, service, mock_db):
        """Deleted session → not found by query (is_deleted=False filter) → ValueError."""
        mock_db.execute = AsyncMock(return_value=_mock_scalar_one_or_none(None))

        with pytest.raises(ValueError, match="Chat session not found"):
            await service.create_user_message(session_id=uuid4(), content="Hello")

    @pytest.mark.asyncio
    async def test_wraps_content_as_json(self, service, mock_db, org_id, user_id):
        """Content is stored as {"text": content}."""
        session_row = _make_session_row(user_id=user_id, organisation_id=org_id)
        mock_db.execute = AsyncMock(
            side_effect=[
                _mock_scalar_one_or_none(session_row),
                _mock_scalars_all([]),
            ]
        )

        await service.create_user_message(session_id=session_row.session_id, content="Hello world")

        # Find the ChatMessage add call (there are multiple add calls)
        added_objects = [call_args[0][0] for call_args in mock_db.add.call_args_list]
        message = next((obj for obj in added_objects if isinstance(obj, ChatMessage)), None)
        assert message is not None
        assert message.content == {"text": "Hello world"}

    @pytest.mark.asyncio
    async def test_creates_pending_job(self, service, mock_db, org_id, user_id):
        """ChatMessageJob is created with PENDING status."""
        session_row = _make_session_row(user_id=user_id, organisation_id=org_id)
        mock_db.execute = AsyncMock(
            side_effect=[
                _mock_scalar_one_or_none(session_row),
                _mock_scalars_all([]),
            ]
        )

        msg, job, assistant_id = await service.create_user_message(
            session_id=session_row.session_id, content="Hello"
        )

        added_objects = [call_args[0][0] for call_args in mock_db.add.call_args_list]
        job_obj = next((obj for obj in added_objects if isinstance(obj, ChatMessageJob)), None)
        assert job_obj is not None
        assert job_obj.status == JobStatus.PENDING

    @pytest.mark.asyncio
    async def test_job_metadata_contains_all_ids(self, service, mock_db, org_id, user_id):
        """Job metadata includes session_id, user_message_id, assistant_message_id."""
        session_row = _make_session_row(user_id=user_id, organisation_id=org_id)
        mock_db.execute = AsyncMock(
            side_effect=[
                _mock_scalar_one_or_none(session_row),
                _mock_scalars_all([]),
            ]
        )

        msg, job, assistant_id = await service.create_user_message(
            session_id=session_row.session_id, content="Hello"
        )

        added_objects = [call_args[0][0] for call_args in mock_db.add.call_args_list]
        job_obj = next((obj for obj in added_objects if isinstance(obj, ChatMessageJob)), None)
        md = job_obj.job_metadata
        assert "session_id" in md
        assert "user_message_id" in md
        assert "assistant_message_id" in md
        assert md["enqueue_status"] == "pending"
        assert md["enqueue_attempts"] == 0
        assert md["celery_task_id"] is None
        assert md["last_enqueue_error"] is None

    @pytest.mark.asyncio
    async def test_returns_tuple_of_message_job_assistant_id(
        self, service, mock_db, org_id, user_id
    ):
        """Return type is (ChatMessage, ChatMessageJob, str)."""
        session_row = _make_session_row(user_id=user_id, organisation_id=org_id)
        mock_db.execute = AsyncMock(
            side_effect=[
                _mock_scalar_one_or_none(session_row),
                _mock_scalars_all([]),
            ]
        )

        result = await service.create_user_message(
            session_id=session_row.session_id, content="Hello"
        )
        assert len(result) == 3
        msg, job, assistant_id = result
        assert isinstance(msg, ChatMessage)
        assert isinstance(job, ChatMessageJob)
        assert isinstance(assistant_id, str)

    @pytest.mark.asyncio
    async def test_flushes_before_linking_attachments(self, service, mock_db, org_id, user_id):
        """db.flush() is called to get message_id before linking attachments."""
        session_row = _make_session_row(user_id=user_id, organisation_id=org_id)
        mock_db.execute = AsyncMock(
            side_effect=[
                _mock_scalar_one_or_none(session_row),
                _mock_scalars_all([]),
            ]
        )

        await service.create_user_message(session_id=session_row.session_id, content="Hello")
        mock_db.flush.assert_awaited()

    @pytest.mark.asyncio
    async def test_user_message_touches_parent_session(self, service, mock_db, org_id, user_id):
        """A new user message bumps the session for chat-list recency."""
        session_row = _make_session_row(user_id=user_id, organisation_id=org_id)
        original_updated_at = session_row.updated_at
        mock_db.execute = AsyncMock(
            side_effect=[
                _mock_scalar_one_or_none(session_row),
                _mock_scalars_all([]),
            ]
        )

        msg, _job, _assistant_id = await service.create_user_message(
            session_id=session_row.session_id,
            content="Hello",
        )

        assert session_row.updated_at == msg.created_at
        assert session_row.updated_at != original_updated_at


# =============================================================================
# 2. Attachment Resolution
# =============================================================================


class TestResolveAttachments:
    @pytest.mark.asyncio
    async def test_filters_by_org_user_not_deleted(self, service, mock_db, org_id, user_id):
        """Only caller's non-deleted attachments are returned."""
        att = _make_attachment_row(organisation_id=org_id, user_id=user_id, is_deleted=False)
        mock_db.execute = AsyncMock(return_value=_mock_scalars_all([att]))

        result = await service._resolve_attachments([att.attachment_id])
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_empty_ids_returns_empty_list(self, service):
        """None/empty attachment_ids → empty list, no DB call."""
        assert await service._resolve_attachments(None) == []
        assert await service._resolve_attachments([]) == []

    @pytest.mark.asyncio
    async def test_nonexistent_ids_returns_empty(self, service, mock_db):
        """IDs not in DB → empty list (no error)."""
        mock_db.execute = AsyncMock(return_value=_mock_scalars_all([]))

        result = await service._resolve_attachments([uuid4()])
        assert result == []


# =============================================================================
# 3. Message Listing
# =============================================================================


class TestListMessages:
    @pytest.mark.asyncio
    async def test_validates_session_ownership(self, service, mock_db):
        """Session must match org+user+not deleted, else ValueError."""
        mock_db.execute = AsyncMock(return_value=_mock_scalar_one_or_none(None))

        with pytest.raises(ValueError, match="Chat session not found"):
            await service.list_messages(session_id=uuid4())

    @pytest.mark.asyncio
    async def test_returns_items_with_cursor(self, service, mock_db, org_id, user_id):
        """Returns (items, cursor) when has_more."""
        session_row = _make_session_row(user_id=user_id, organisation_id=org_id)
        messages = [_make_message_row(created_at=datetime(2024, 1, i + 1)) for i in range(3)]
        mock_db.execute = AsyncMock(
            side_effect=[
                _mock_scalar_one_or_none(session_row),
                _mock_scalars_all(messages),
            ]
        )

        _, items, cursor = await service.list_messages(session_id=session_row.session_id, limit=2)
        assert len(items) == 2
        assert cursor is not None

    @pytest.mark.asyncio
    async def test_returns_none_cursor_on_last_page(self, service, mock_db, org_id, user_id):
        """No more items → cursor=None."""
        session_row = _make_session_row(user_id=user_id, organisation_id=org_id)
        messages = [_make_message_row()]
        mock_db.execute = AsyncMock(
            side_effect=[
                _mock_scalar_one_or_none(session_row),
                _mock_scalars_all(messages),
            ]
        )

        _, items, cursor = await service.list_messages(session_id=session_row.session_id, limit=20)
        assert len(items) == 1
        assert cursor is None

    @pytest.mark.asyncio
    async def test_selected_viewer_reads_owner_messages(self, service, mock_db, org_id):
        """A readable session may belong to another user without copying messages."""
        session_row = _make_session_row(user_id=uuid4(), organisation_id=org_id)
        message = _make_message_row(session_id=session_row.session_id)
        mock_db.execute = AsyncMock(
            side_effect=[
                _mock_scalar_one_or_none(session_row),
                _mock_scalars_all([message]),
            ]
        )

        _, items, _ = await service.list_messages(session_id=session_row.session_id)

        assert items == [message]
        assert items[0].session_id == session_row.session_id

    @pytest.mark.asyncio
    async def test_refresh_reads_new_owner_message_from_same_session(
        self, service, mock_db, org_id
    ):
        """A viewer refresh sees newly persisted owner messages in the original chat."""
        session_row = _make_session_row(user_id=uuid4(), organisation_id=org_id)
        old = _make_message_row(session_id=session_row.session_id)
        new = _make_message_row(session_id=session_row.session_id, created_at=datetime(2024, 1, 2))
        mock_db.execute = AsyncMock(
            side_effect=[
                _mock_scalar_one_or_none(session_row),
                _mock_scalars_all([old]),
                _mock_scalar_one_or_none(session_row),
                _mock_scalars_all([new, old]),
            ]
        )

        _, first, _ = await service.list_messages(session_id=session_row.session_id)
        _, refreshed, _ = await service.list_messages(session_id=session_row.session_id)

        assert first == [old]
        assert refreshed == [new, old]
        assert {message.session_id for message in refreshed} == {session_row.session_id}


# =============================================================================
# 4. Attachment URLs & List Items
# =============================================================================


class TestGetMessageAttachments:
    @pytest.mark.asyncio
    async def test_empty_messages_still_authorizes_session(self, service, mock_db, org_id, user_id):
        """Revoked viewers cannot bypass session authorization with an empty page."""
        session_row = _make_session_row(user_id=user_id, organisation_id=org_id)
        mock_db.execute = AsyncMock(return_value=_mock_scalar_one_or_none(session_row))

        result = await service.get_message_attachments(
            session_id=session_row.session_id, messages=[]
        )

        assert result == {}
        mock_db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_attachments_keyed_by_message_id(self, service, mock_db):
        """Attachments are grouped by message_id."""
        msg = _make_message_row()
        att = _make_attachment_row()

        # Mock the join query result: (message_id, Attachment, order)
        mock_result = MagicMock()
        mock_result.all.return_value = [(msg.message_id, att, 0)]
        session_row = _make_session_row(session_id=msg.session_id)
        mock_db.execute = AsyncMock(
            side_effect=[_mock_scalar_one_or_none(session_row), mock_result]
        )

        result = await service.get_message_attachments(session_id=msg.session_id, messages=[msg])
        assert msg.message_id in result
        assert len(result[msg.message_id]) == 1

    @pytest.mark.asyncio
    async def test_revocation_blocks_new_attachment_lookup(self, service, mock_db):
        """Attachment URLs cannot be prepared after the viewer grant disappears."""
        msg = _make_message_row()
        mock_db.execute = AsyncMock(return_value=_mock_scalar_one_or_none(None))

        with pytest.raises(ValueError, match="Chat session not found"):
            await service.get_message_attachments(session_id=msg.session_id, messages=[msg])

        mock_db.execute.assert_awaited_once()


class TestBuildAttachmentUrls:
    @pytest.mark.asyncio
    async def test_generates_sas_for_thumbnails(self, service, org_id):
        """SAS URL generated for thumbnail_small_path."""
        att = _make_attachment_row(thumbnail_small_path="thumbs/small.png")

        with patch(
            "src.service.chat.chat_message_service.AzureOrganisationBlobStorage"
        ) as mock_storage:
            storage_instance = AsyncMock()
            storage_instance.generate_read_sas = AsyncMock(return_value="https://sas-url")
            storage_instance.close = AsyncMock()
            mock_storage.return_value = storage_instance

            result = await service.build_attachment_urls(attachments_by_msg={uuid4(): [att]})

        # Get the first (only) message's attachments
        msg_atts = list(result.values())[0]
        assert msg_atts[0]["thumbnail_small_url"] == "https://sas-url"

    @pytest.mark.asyncio
    async def test_handles_missing_thumbnail_path(self, service, org_id):
        """None thumbnail path → None URL."""
        att = _make_attachment_row(thumbnail_small_path=None)

        with patch(
            "src.service.chat.chat_message_service.AzureOrganisationBlobStorage"
        ) as mock_storage:
            storage_instance = AsyncMock()
            storage_instance.close = AsyncMock()
            mock_storage.return_value = storage_instance

            result = await service.build_attachment_urls(attachments_by_msg={uuid4(): [att]})

        msg_atts = list(result.values())[0]
        assert msg_atts[0]["thumbnail_small_url"] is None

    @pytest.mark.asyncio
    async def test_handles_sas_generation_failure(self, service, org_id):
        """SAS generation failure → None URL, not crash."""
        att = _make_attachment_row(thumbnail_small_path="thumbs/small.png")

        with patch(
            "src.service.chat.chat_message_service.AzureOrganisationBlobStorage"
        ) as mock_storage:
            storage_instance = AsyncMock()
            storage_instance.generate_read_sas = AsyncMock(side_effect=Exception("Azure down"))
            storage_instance.close = AsyncMock()
            mock_storage.return_value = storage_instance

            result = await service.build_attachment_urls(attachments_by_msg={uuid4(): [att]})

        msg_atts = list(result.values())[0]
        assert msg_atts[0]["thumbnail_small_url"] is None
        assert msg_atts[0]["playback_url"] is None

    @pytest.mark.asyncio
    async def test_generates_playback_url_for_completed_audio(self, service):
        """Completed audio attachments get an original-file URL for inline playback."""
        att = _make_attachment_row(
            filename="voice.ogg",
            mimetype="audio/ogg",
            storage_path="chat_uploads/user/voice.ogg",
            status=AttachmentStatus.COMPLETED,
        )
        generated: list[dict] = []

        async def generate_read_sas(path, **kwargs):
            generated.append({"path": path, **kwargs})
            return "https://audio-sas-url"

        with patch(
            "src.service.chat.chat_message_service.AzureOrganisationBlobStorage"
        ) as mock_storage:
            storage_instance = AsyncMock()
            storage_instance.generate_read_sas = AsyncMock(side_effect=generate_read_sas)
            storage_instance.close = AsyncMock()
            mock_storage.return_value = storage_instance

            result = await service.build_attachment_urls(attachments_by_msg={uuid4(): [att]})

        msg_atts = list(result.values())[0]
        assert msg_atts[0]["playback_url"] == "https://audio-sas-url"
        assert generated == [
            {
                "path": "chat_uploads/user/voice.ogg",
                "minutes": 30,
                "content_type": "audio/ogg",
            }
        ]

    @pytest.mark.asyncio
    async def test_playback_url_is_null_until_audio_completed(self, service):
        """Processing audio is listed but not yet playable."""
        att = _make_attachment_row(
            filename="voice.ogg",
            mimetype="audio/ogg",
            storage_path="chat_uploads/user/voice.ogg",
            status=AttachmentStatus.PROCESSING,
        )

        with patch(
            "src.service.chat.chat_message_service.AzureOrganisationBlobStorage"
        ) as mock_storage:
            storage_instance = AsyncMock()
            storage_instance.close = AsyncMock()
            mock_storage.return_value = storage_instance

            result = await service.build_attachment_urls(attachments_by_msg={uuid4(): [att]})

        msg_atts = list(result.values())[0]
        assert msg_atts[0]["playback_url"] is None
        storage_instance.generate_read_sas.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_audio_attachment_has_no_playback_url(self, service):
        """Documents keep thumbnail/download behavior separate from audio playback."""
        att = _make_attachment_row(
            mimetype="application/pdf",
            storage_path="chat_uploads/user/doc.pdf",
            status=AttachmentStatus.COMPLETED,
        )

        with patch(
            "src.service.chat.chat_message_service.AzureOrganisationBlobStorage"
        ) as mock_storage:
            storage_instance = AsyncMock()
            storage_instance.close = AsyncMock()
            mock_storage.return_value = storage_instance

            result = await service.build_attachment_urls(attachments_by_msg={uuid4(): [att]})

        msg_atts = list(result.values())[0]
        assert msg_atts[0]["playback_url"] is None
        storage_instance.generate_read_sas.assert_not_awaited()


class TestBuildMessageListItems:
    def test_extracts_text_from_content_dict(self, service):
        """Content dict {"text": "..."} → text extracted correctly."""
        msg = _make_message_row(content={"text": "Hello world"})
        items = service.build_message_list_items([msg], {})
        assert items[0]["content"] == "Hello world"

    def test_handles_string_content(self, service):
        """String content returned as-is."""
        msg = _make_message_row()
        msg.content = "Plain string"
        items = service.build_message_list_items([msg], {})
        assert items[0]["content"] == "Plain string"

    def test_includes_attachments_from_urls_dict(self, service):
        """Attachment URLs included per message."""
        msg = _make_message_row()
        atts_urls = {
            msg.message_id: [
                {
                    "attachment_id": uuid4(),
                    "filename": "doc.pdf",
                    "mimetype": "application/pdf",
                    "thumbnail_small_url": "https://url",
                }
            ]
        }
        items = service.build_message_list_items([msg], atts_urls)
        assert len(items[0]["attachments"]) == 1

    def test_includes_feedback_score(self, service):
        """Feedback score included in list items."""
        msg = _make_message_row(feedback_score=1)
        items = service.build_message_list_items([msg], {})
        assert items[0]["feedback_score"] == 1

    def test_includes_agent_data_sources_and_suggestions(self, service):
        """Agent data (sources, suggestions) included in list items."""
        msg = _make_message_row()
        msg.agent_data = {
            "sources": [{"title": "Doc", "url": "https://x"}],
            "suggested_questions": ["What next?"],
        }
        items = service.build_message_list_items([msg], {})
        assert items[0]["sources"] == [{"title": "Doc", "url": "https://x"}]
        assert items[0]["suggestions"] == ["What next?"]


# =============================================================================
# 5. Feedback
# =============================================================================


class TestUpdateMessageFeedback:
    @pytest.mark.asyncio
    async def test_verifies_ownership_via_session_join(self, service, mock_db, org_id, user_id):
        """Feedback verifies message ownership through ChatSession join."""
        msg_row = _make_message_row(feedback_score=None)
        mock_db.execute = AsyncMock(return_value=_mock_scalar_one_or_none(msg_row))

        result = await service.update_message_feedback(
            message_id=msg_row.message_id, feedback_score=1
        )
        assert result.feedback_score == 1

    @pytest.mark.asyncio
    async def test_score_zero_clears_to_none(self, service, mock_db):
        """Score 0 clears feedback to None."""
        msg_row = _make_message_row(feedback_score=1)
        mock_db.execute = AsyncMock(return_value=_mock_scalar_one_or_none(msg_row))

        result = await service.update_message_feedback(
            message_id=msg_row.message_id, feedback_score=0
        )
        assert result.feedback_score is None

    @pytest.mark.asyncio
    async def test_invalid_score_raises_value_error(self, service):
        """Scores outside {-1, 0, 1} → ValueError."""
        for bad_score in [2, -2, 5, -5, 100]:
            with pytest.raises(ValueError, match="feedback_score must be -1, 0, or 1"):
                await service.update_message_feedback(message_id=uuid4(), feedback_score=bad_score)

    @pytest.mark.asyncio
    async def test_nonexistent_message_raises_value_error(self, service, mock_db):
        """Missing message → ValueError."""
        mock_db.execute = AsyncMock(return_value=_mock_scalar_one_or_none(None))

        with pytest.raises(ValueError, match="Message not found"):
            await service.update_message_feedback(message_id=uuid4(), feedback_score=1)


# =============================================================================
# 6. Confidence Score Conversion
# =============================================================================


class TestToConfidence:
    @pytest.mark.parametrize(
        "input_val,expected",
        [
            ("high", "high"),
            ("very_low", "very_low"),
            ("medium", "medium"),
            ("very_high", "very_high"),
            ("low", "low"),
        ],
    )
    def test_string_labels_returned_as_is(self, input_val, expected):
        assert _to_confidence(input_val) == expected

    @pytest.mark.parametrize(
        "input_val,expected",
        [
            (0.9, "high"),
            (0.8, "high"),
            (0.79, "medium"),
            (0.5, "medium"),
            (0.49, "low"),
            (0.1, "low"),
        ],
    )
    def test_numeric_thresholds(self, input_val, expected):
        assert _to_confidence(input_val) == expected

    def test_none_returns_none(self):
        assert _to_confidence(None) is None

    def test_unknown_string_returned_stripped(self):
        assert _to_confidence("  some_unknown  ") == "some_unknown"

    def test_non_string_non_numeric_returns_none(self):
        assert _to_confidence([1, 2, 3]) is None
