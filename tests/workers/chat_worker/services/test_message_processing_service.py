"""Tests for MessageProcessingService core process flow.

Covers job loading, agent workflow execution, result saving, frame processing,
and error handling — the critical worker pipeline that existing tests skip.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables.chat_message_models import (
    ChatMessage,
    ChatMessageRole,
    ChatMessageType,
)
from src.domain.models.organisation_tables.chat_session_models import ChatSession
from src.domain.models.organisation_tables.job_models import ChatMessageJob, JobStatus
from src.workers.chat_worker.services.message_processing_service import (
    FRAME_TYPE_ANSWER_DELTA,
    FRAME_TYPE_ANSWER_METRICS,
    FRAME_TYPE_ANSWER_REPLACE,
    FRAME_TYPE_FINALIZE,
    FRAME_TYPE_PROGRESS_UPDATE,
    FRAME_TYPE_QUESTION_PROMPT,
    FRAME_TYPE_SOURCES_REPLACE,
    FRAME_TYPE_STRUCTURED_DATA,
    FRAME_TYPE_SUGGESTIONS_REPLACE,
    FRAME_TYPE_TASK_LIST,
    FRAME_TYPE_THINKING_UPDATE,
    FRAME_TYPE_TOKEN_USAGE,
    FRAME_TYPE_TOOL_USAGE,
    FRAME_TYPE_VISUALIZATION,
    AgentResult,
    ConversationData,
    MessageProcessingService,
    RedisPublisher,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job(
    *,
    job_id: UUID | None = None,
    status: JobStatus = JobStatus.PENDING,
    session_id: UUID | None = None,
    user_message_id: UUID | None = None,
    assistant_message_id: UUID | None = None,
    user_id: UUID | None = None,
) -> MagicMock:
    job = MagicMock(spec=ChatMessageJob)
    job.job_id = job_id or uuid4()
    job.status = status
    job.job_metadata = {
        "session_id": str(session_id or uuid4()),
        "user_message_id": str(user_message_id or uuid4()),
        "assistant_message_id": str(assistant_message_id or uuid4()),
    }
    job.user_id = user_id
    job.error_message = None
    job.result_data = None
    job.token_usage = None
    job.model_name = None
    job.cost = None
    job.latency_ms = None
    return job


def _make_chat_session(
    *,
    session_id: UUID | None = None,
    user_id: UUID | None = None,
    chat_type: str = "GENERAL_CHAT",
) -> MagicMock:
    session = MagicMock(spec=ChatSession)
    session.session_id = session_id or uuid4()
    session.user_id = user_id or uuid4()
    session.chat_type = chat_type
    return session


def _make_chat_message(
    *,
    message_id: UUID | None = None,
    session_id: UUID | None = None,
    role: ChatMessageRole = ChatMessageRole.USER,
    message_type: ChatMessageType = ChatMessageType.GENERAL_MESSAGE,
) -> MagicMock:
    msg = MagicMock(spec=ChatMessage)
    msg.message_id = message_id or uuid4()
    msg.session_id = session_id or uuid4()
    msg.role = role
    msg.message_type = message_type
    msg.content = {"text": "Hello"}
    msg.created_at = None
    msg.parent_message_id = None
    return msg


def _make_conversation_data(**overrides) -> ConversationData:
    defaults = {
        "job_id": uuid4(),
        "session_id": uuid4(),
        "user_message_id": uuid4(),
        "assistant_message_id": uuid4(),
        "chat_type": "GENERAL_CHAT",
        "user_id": uuid4(),
        "user_message_session_id": uuid4(),
        "user_message_parent_id": uuid4(),
        "user_message_type": ChatMessageType.GENERAL_MESSAGE,
        "user_email": "test@example.com",
        "history": [],
        "attachments": [],
    }
    defaults.update(overrides)
    return ConversationData(**defaults)


def _make_result(**overrides) -> AgentResult:
    defaults = {
        "answer": "The answer is 42.",
        "confidence": 0.85,
        "suggestions": ["What else?"],
        "sources": [{"title": "Doc", "url": "https://x"}],
    }
    defaults.update(overrides)
    return AgentResult(**defaults)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def org_id():
    return uuid4()


@pytest.fixture
def service(org_id):
    return MessageProcessingService(organisation_id=org_id)


# =============================================================================
# 1. Job Loading (_load_job_data)
# =============================================================================


class TestLoadJobData:
    @patch("src.workers.chat_worker.services.message_processing_service.acquire_master_session")
    @patch("src.workers.chat_worker.services.message_processing_service.acquire_org_session")
    def test_marks_job_in_progress(self, mock_acquire_org, mock_acquire_master, service):
        """Job status transitions to IN_PROGRESS on load."""
        job_id = uuid4()
        session_id = uuid4()
        user_msg_id = uuid4()
        asst_msg_id = uuid4()

        job = _make_job(
            job_id=job_id,
            session_id=session_id,
            user_message_id=user_msg_id,
            assistant_message_id=asst_msg_id,
        )
        chat_session = _make_chat_session(session_id=session_id)
        user_message = _make_chat_message(message_id=user_msg_id, session_id=session_id)

        mock_session = MagicMock()
        mock_session.get.side_effect = (
            lambda cls, obj_id: {
                ChatMessageJob: {job_id: job},
                ChatSession: {session_id: chat_session},
                ChatMessage: {user_msg_id: user_message},
            }.get(cls, {}).get(obj_id)
        )
        mock_session.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        mock_acquire_org.return_value = iter([mock_session])
        mock_acquire_master.return_value = iter([MagicMock()])

        service._load_job_data(job_id)
        assert job.status == JobStatus.IN_PROGRESS

    @patch("src.workers.chat_worker.services.message_processing_service.acquire_master_session")
    @patch("src.workers.chat_worker.services.message_processing_service.acquire_org_session")
    def test_skips_job_that_is_no_longer_pending(
        self, mock_acquire_org, mock_acquire_master, service
    ):
        """A redelivered task cannot reopen a completed web-chat job."""
        job_id = uuid4()
        session_id = uuid4()
        user_msg_id = uuid4()
        job = _make_job(
            job_id=job_id,
            status=JobStatus.COMPLETED,
            session_id=session_id,
            user_message_id=user_msg_id,
        )
        chat_session = _make_chat_session(session_id=session_id)
        user_message = _make_chat_message(message_id=user_msg_id, session_id=session_id)

        mock_session = MagicMock()
        mock_session.get.side_effect = (
            lambda cls, obj_id: {
                ChatMessageJob: {job_id: job},
                ChatSession: {session_id: chat_session},
                ChatMessage: {user_msg_id: user_message},
            }.get(cls, {}).get(obj_id)
        )
        mock_session.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        mock_acquire_org.return_value = iter([mock_session])
        mock_acquire_master.return_value = iter([MagicMock()])

        result = service._load_job_data(job_id)

        assert result is None
        assert job.status == JobStatus.COMPLETED
        mock_session.refresh.assert_called_once_with(job, with_for_update=True)

    @patch("src.workers.chat_worker.services.message_processing_service.acquire_org_session")
    def test_returns_none_when_job_not_found_without_celery(self, mock_acquire_org, service):
        """No celery_task → return None when job missing."""
        mock_session = MagicMock()
        mock_session.get.return_value = None
        mock_acquire_org.return_value = iter([mock_session])

        result = service._load_job_data(uuid4(), celery_task=None)
        assert result is None

    @patch("src.workers.chat_worker.services.message_processing_service.acquire_org_session")
    def test_retries_when_job_not_found_with_celery_task(self, mock_acquire_org, service):
        """With celery_task → raises celery_task.retry on missing job."""
        mock_session = MagicMock()
        mock_session.get.return_value = None
        mock_acquire_org.return_value = iter([mock_session])

        celery_task = MagicMock()
        celery_task.request.retries = 0
        celery_task.retry.side_effect = RuntimeError("Celery retry")

        with pytest.raises(RuntimeError, match="Celery retry"):
            service._load_job_data(uuid4(), celery_task=celery_task)

        celery_task.retry.assert_called_once()

    @patch("src.workers.chat_worker.services.message_processing_service.acquire_org_session")
    def test_raises_on_missing_metadata_ids(self, mock_acquire_org, service):
        """Missing session_id/user_message_id/assistant_message_id → RuntimeError."""
        job = MagicMock(spec=ChatMessageJob)
        job.job_id = uuid4()
        job.status = JobStatus.PENDING
        job.job_metadata = {}  # Missing all required IDs

        mock_session = MagicMock()
        mock_session.get.return_value = job
        mock_acquire_org.return_value = iter([mock_session])

        with pytest.raises(RuntimeError, match="Missing required IDs"):
            service._load_job_data(job.job_id)

    @patch("src.workers.chat_worker.services.message_processing_service.acquire_org_session")
    def test_raises_on_invalid_uuid_in_metadata(self, mock_acquire_org, service):
        """Non-UUID strings in metadata → RuntimeError."""
        job = MagicMock(spec=ChatMessageJob)
        job.job_id = uuid4()
        job.status = JobStatus.PENDING
        job.job_metadata = {
            "session_id": "not-a-uuid",
            "user_message_id": "also-bad",
            "assistant_message_id": "nope",
        }

        mock_session = MagicMock()
        mock_session.get.return_value = job
        mock_acquire_org.return_value = iter([mock_session])

        with pytest.raises(RuntimeError, match="Invalid UUID"):
            service._load_job_data(job.job_id)

    @patch("src.workers.chat_worker.services.message_processing_service.acquire_org_session")
    def test_raises_when_session_not_found(self, mock_acquire_org, service):
        """Missing ChatSession → RuntimeError."""
        session_id = uuid4()
        job = _make_job(session_id=session_id)

        mock_session = MagicMock()
        mock_session.get.side_effect = lambda cls, obj_id: (job if cls == ChatMessageJob else None)
        mock_acquire_org.return_value = iter([mock_session])

        with pytest.raises(RuntimeError, match="Session .* not found"):
            service._load_job_data(job.job_id)

    @patch("src.workers.chat_worker.services.message_processing_service.acquire_org_session")
    def test_raises_when_user_message_not_found(self, mock_acquire_org, service):
        """Missing user ChatMessage → RuntimeError."""
        session_id = uuid4()
        user_msg_id = uuid4()
        job = _make_job(session_id=session_id, user_message_id=user_msg_id)
        chat_session = _make_chat_session(session_id=session_id)

        def mock_get(cls, obj_id):
            if cls == ChatMessageJob:
                return job
            if cls == ChatSession and obj_id == session_id:
                return chat_session
            return None  # User message not found

        mock_session = MagicMock()
        mock_session.get.side_effect = mock_get
        mock_acquire_org.return_value = iter([mock_session])

        with pytest.raises(RuntimeError, match="User message .* not found"):
            service._load_job_data(job.job_id)


# =============================================================================
# 2. Frame Processing (_process_frame_data)
# =============================================================================


class TestProcessFrameData:
    @pytest.fixture
    def result_summary(self):
        return {
            "answer_parts": [],
            "final_answer": None,
            "confidence": None,
            "suggestions": None,
            "sources": None,
            "user_questions": [],
            "progress_updates": [],
            "tool_usage": [],
            "thinking_updates": [],
            "visualizations": [],
            "structured_data": [],
            "task_list": [],
            "token_usage": None,
            "model_name": None,
            "cost": None,
            "duration_seconds": None,
        }

    def test_answer_delta_accumulates_parts(self, service, result_summary):
        """Multiple answer.delta frames concatenated."""
        service._process_frame_data(
            {"type": FRAME_TYPE_ANSWER_DELTA, "content": "Hello "}, result_summary
        )
        service._process_frame_data(
            {"type": FRAME_TYPE_ANSWER_DELTA, "content": "world"}, result_summary
        )
        assert result_summary["answer_parts"] == ["Hello ", "world"]

    def test_answer_replace_overwrites(self, service, result_summary):
        """answer.replace sets final_answer."""
        service._process_frame_data(
            {"type": FRAME_TYPE_ANSWER_REPLACE, "content": "Final answer"}, result_summary
        )
        assert result_summary["final_answer"] == "Final answer"

    def test_answer_metrics_captures_confidence(self, service, result_summary):
        """answer.metrics stores confidence score."""
        service._process_frame_data(
            {"type": FRAME_TYPE_ANSWER_METRICS, "confidence": 0.85}, result_summary
        )
        assert result_summary["confidence"] == 0.85

    def test_suggestions_extracts_string_items(self, service, result_summary):
        """String suggestion items extracted as-is."""
        service._process_frame_data(
            {"type": FRAME_TYPE_SUGGESTIONS_REPLACE, "items": ["Q1", "Q2"]}, result_summary
        )
        assert result_summary["suggestions"] == ["Q1", "Q2"]

    def test_suggestions_extracts_dict_labels(self, service, result_summary):
        """Dict suggestion items extract label/text/title."""
        service._process_frame_data(
            {
                "type": FRAME_TYPE_SUGGESTIONS_REPLACE,
                "items": [{"label": "Option A"}, {"text": "Option B"}],
            },
            result_summary,
        )
        assert result_summary["suggestions"] == ["Option A", "Option B"]

    def test_sources_replace_stored(self, service, result_summary):
        """Sources stored directly."""
        sources = [{"title": "Doc", "url": "https://x"}]
        service._process_frame_data(
            {"type": FRAME_TYPE_SOURCES_REPLACE, "sources": sources}, result_summary
        )
        assert result_summary["sources"] == sources

    def test_question_prompt_appended(self, service, result_summary):
        """Question prompt appended with correct fields."""
        service._process_frame_data(
            {
                "type": FRAME_TYPE_QUESTION_PROMPT,
                "prompt": "What is your deal?",
                "fields": [{"name": "deal_name"}],
                "await_user_response": True,
            },
            result_summary,
        )
        assert len(result_summary["user_questions"]) == 1
        assert result_summary["user_questions"][0]["prompt"] == "What is your deal?"

    def test_progress_update_appended(self, service, result_summary):
        """Progress update appended."""
        service._process_frame_data(
            {
                "type": FRAME_TYPE_PROGRESS_UPDATE,
                "stage": "searching",
                "message": "Looking...",
                "progress": 0.5,
            },
            result_summary,
        )
        assert len(result_summary["progress_updates"]) == 1
        assert result_summary["progress_updates"][0]["stage"] == "searching"

    def test_tool_usage_appended(self, service, result_summary):
        """Tool usage appended."""
        service._process_frame_data(
            {
                "type": FRAME_TYPE_TOOL_USAGE,
                "tool_name": "search_knowledge",
                "status": "completed",
                "duration_ms": 120,
            },
            result_summary,
        )
        assert len(result_summary["tool_usage"]) == 1
        assert result_summary["tool_usage"][0]["tool_name"] == "search_knowledge"

    def test_thinking_update_appended(self, service, result_summary):
        """Thinking update appended."""
        service._process_frame_data(
            {"type": FRAME_TYPE_THINKING_UPDATE, "thought": "Analyzing...", "stage": "reasoning"},
            result_summary,
        )
        assert len(result_summary["thinking_updates"]) == 1

    def test_visualization_appended(self, service, result_summary):
        """Visualization data captured."""
        service._process_frame_data(
            {
                "type": FRAME_TYPE_VISUALIZATION,
                "visualization_type": "chart",
                "diagram_type": "bar",
                "content": "data",
                "title": "Revenue",
            },
            result_summary,
        )
        assert len(result_summary["visualizations"]) == 1
        assert result_summary["visualizations"][0]["title"] == "Revenue"

    def test_structured_data_appended(self, service, result_summary):
        """Structured data captured."""
        service._process_frame_data(
            {
                "type": FRAME_TYPE_STRUCTURED_DATA,
                "data_type": "table",
                "title": "Deals",
                "columns": ["Name", "Value"],
                "rows": [["Deal1", "100"]],
            },
            result_summary,
        )
        assert len(result_summary["structured_data"]) == 1

    def test_task_list_filters_non_dicts(self, service, result_summary):
        """Non-dict task items are skipped."""
        service._process_frame_data(
            {
                "type": FRAME_TYPE_TASK_LIST,
                "tasks": [
                    {"id": "1", "task": "Do X", "status": "done"},
                    "not-a-dict",
                    42,
                ],
            },
            result_summary,
        )
        assert len(result_summary["task_list"]) == 1
        assert result_summary["task_list"][0]["task"] == "Do X"

    def test_token_usage_captures_all_fields(self, service, result_summary):
        """All 6 token fields + model + cost + duration captured."""
        service._process_frame_data(
            {
                "type": FRAME_TYPE_TOKEN_USAGE,
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
                "reasoning_tokens": 20,
                "cache_read_tokens": 10,
                "cache_write_tokens": 5,
                "model_name": "gpt-4",
                "cost": 0.005,
                "duration_seconds": 3.2,
            },
            result_summary,
        )
        assert result_summary["token_usage"]["input_tokens"] == 100
        assert result_summary["token_usage"]["output_tokens"] == 50
        assert result_summary["token_usage"]["total_tokens"] == 150
        assert result_summary["token_usage"]["reasoning_tokens"] == 20
        assert result_summary["model_name"] == "gpt-4"
        assert result_summary["cost"] == 0.005
        assert result_summary["duration_seconds"] == 3.2

    def test_unknown_frame_type_ignored(self, service, result_summary):
        """Unknown frame types don't crash."""
        service._process_frame_data({"type": "unknown.frame", "data": "whatever"}, result_summary)
        # No changes to summary
        assert result_summary["answer_parts"] == []
        assert result_summary["confidence"] is None


class TestExecuteAgentWorkflow:
    """Validate agent output before it reaches durable persistence."""

    @patch("src.workers.chat_worker.services.message_processing_service.run_async")
    def test_empty_answer_uses_fallback_without_discarding_metrics(self, mock_run_async, service):
        """A completed model run must not persist an empty assistant response."""
        token_usage = {
            "input_tokens": 12476,
            "output_tokens": 89,
            "total_tokens": 12565,
        }
        result_summary = {
            "answer": None,
            "token_usage": token_usage,
            "model_name": "gpt-5.4",
            "cost": 0.032525,
            "duration_seconds": 2.516,
        }

        def consume_coroutine(coroutine):
            """Close the unused coroutine and return the simulated stream summary."""
            coroutine.close()
            return result_summary

        mock_run_async.side_effect = consume_coroutine
        result = service._execute_agent_workflow(_make_conversation_data())

        assert result.answer
        assert "error" in result.answer.lower() or "apologize" in result.answer.lower()
        assert result.token_usage == token_usage
        assert result.model_name == "gpt-5.4"
        assert result.cost == 0.032525
        assert result.duration_seconds == 2.516


# =============================================================================
# 3. Result Saving (_save_results)
# =============================================================================


class TestSaveResults:
    @patch("src.workers.chat_worker.services.message_processing_service.acquire_org_session")
    def test_creates_assistant_message_with_correct_fields(self, mock_acquire_org, service):
        """Assistant message created with all required fields."""
        data = _make_conversation_data()
        result = _make_result()

        mock_session = MagicMock()
        mock_session.get.return_value = _make_job()  # Returns a job for finalization
        mock_acquire_org.return_value = iter([mock_session])
        service.publisher = MagicMock()

        service._save_results(data, result)

        # Find the ChatMessage that was added
        added_objects = [call_args[0][0] for call_args in mock_session.add.call_args_list]
        assistant_msg = next((obj for obj in added_objects if isinstance(obj, ChatMessage)), None)
        assert assistant_msg is not None
        assert assistant_msg.message_id == data.assistant_message_id
        assert assistant_msg.role == ChatMessageRole.ASSISTANT
        assert assistant_msg.content == {"text": "The answer is 42."}

    @patch("src.workers.chat_worker.services.message_processing_service.acquire_org_session")
    def test_sets_parent_message_to_user_message(self, mock_acquire_org, service):
        """Parent message ID is set for threading."""
        user_msg_id = uuid4()
        data = _make_conversation_data(user_message_parent_id=user_msg_id)
        result = _make_result()

        mock_session = MagicMock()
        mock_session.get.return_value = _make_job()
        mock_acquire_org.return_value = iter([mock_session])
        service.publisher = MagicMock()

        service._save_results(data, result)

        added_objects = [call_args[0][0] for call_args in mock_session.add.call_args_list]
        assistant_msg = next((obj for obj in added_objects if isinstance(obj, ChatMessage)), None)
        assert assistant_msg.parent_message_id == user_msg_id

    @patch("src.workers.chat_worker.services.message_processing_service.acquire_org_session")
    def test_finalizes_job_with_completed_status(self, mock_acquire_org, service):
        """Job is marked COMPLETED with result_data."""
        data = _make_conversation_data()
        result = _make_result()

        job = _make_job()
        mock_session = MagicMock()
        mock_session.get.return_value = job
        mock_acquire_org.return_value = iter([mock_session])
        service.publisher = MagicMock()

        service._save_results(data, result)

        assert job.status == JobStatus.COMPLETED
        assert job.result_data["answer"] == "The answer is 42."

    @patch("src.workers.chat_worker.services.message_processing_service.acquire_org_session")
    def test_persists_token_usage_and_cost(self, mock_acquire_org, service):
        """Token usage, cost (Decimal), latency_ms saved to job."""
        data = _make_conversation_data()
        result = _make_result(
            token_usage={"input_tokens": 100, "output_tokens": 50},
            model_name="gpt-4",
            cost=0.005,
            duration_seconds=3.2,
        )

        job = _make_job()
        mock_session = MagicMock()
        mock_session.get.return_value = job
        mock_acquire_org.return_value = iter([mock_session])
        service.publisher = MagicMock()

        service._save_results(data, result)

        assert job.token_usage == {"input_tokens": 100, "output_tokens": 50}
        assert job.model_name == "gpt-4"
        assert job.cost == Decimal("0.005")
        assert job.latency_ms == 3200

    @patch("src.workers.chat_worker.services.message_processing_service.acquire_org_session")
    def test_publishes_finalize_frame(self, mock_acquire_org, service):
        """Finalize frame published to Redis after save."""
        data = _make_conversation_data()
        result = _make_result()

        mock_session = MagicMock()
        mock_session.get.return_value = _make_job()
        mock_acquire_org.return_value = iter([mock_session])
        service.publisher = MagicMock()

        service._save_results(data, result)

        # Check that a finalize frame was published
        publish_calls = service.publisher.publish.call_args_list
        finalize_frames = [
            call_args[0][0]
            for call_args in publish_calls
            if call_args[0][0].get("type") == FRAME_TYPE_FINALIZE
        ]
        assert len(finalize_frames) >= 1
        assert finalize_frames[-1]["reason"] == "done"


# =============================================================================
# 4. Error Handling & Job Failure
# =============================================================================


class TestErrorHandling:
    def test_publish_failure_frames_closes_live_stream(self, service):
        """A terminal worker failure tells connected clients to stop waiting."""
        service.publisher = MagicMock()

        service._publish_failure_frames(uuid4(), "Agent crashed")

        frames = [call.args[0] for call in service.publisher.publish.call_args_list]
        assert [frame["type"] for frame in frames] == ["error", "finalize"]
        assert frames[0]["message"] == "Agent crashed"
        assert frames[1]["reason"] == "error"

    @patch("src.workers.chat_worker.services.message_processing_service.acquire_org_session")
    def test_mark_job_failed_sets_status(self, mock_acquire_org, service):
        """_mark_job_failed sets job status to FAILED with error message."""
        job = _make_job()
        mock_session = MagicMock()
        mock_session.get.return_value = job
        mock_acquire_org.return_value = iter([mock_session])

        service._mark_job_failed(job.job_id, "Something went wrong")

        assert job.status == JobStatus.FAILED
        assert job.error_message == "Something went wrong"

    @patch("src.workers.chat_worker.services.message_processing_service.acquire_org_session")
    def test_mark_job_failed_truncates_error_to_500_chars(self, mock_acquire_org, service):
        """Long error messages truncated to 500 chars."""
        job = _make_job()
        mock_session = MagicMock()
        mock_session.get.return_value = job
        mock_acquire_org.return_value = iter([mock_session])

        long_error = "x" * 1000
        service._mark_job_failed(job.job_id, long_error)

        assert len(job.error_message) == 500

    @patch("src.workers.chat_worker.services.message_processing_service.acquire_org_session")
    def test_mark_job_failed_silently_handles_db_failure(self, mock_acquire_org, service):
        """DB error during failure marking → logged, not re-raised."""
        mock_acquire_org.return_value = iter([])  # Empty iterator → no session

        # Should not raise
        service._mark_job_failed(uuid4(), "error")

    def test_create_error_result_returns_apology(self, service):
        """Error result contains apology message."""
        result = service._create_error_result()
        assert "error" in result.answer.lower() or "apologize" in result.answer.lower()


# =============================================================================
# 5. Full Process Flow
# =============================================================================


class TestProcessFlow:
    @patch(
        "src.workers.chat_worker.services.message_processing_service.MessageProcessingService._publish_final_frames"
    )
    @patch(
        "src.workers.chat_worker.services.message_processing_service.MessageProcessingService._save_results"
    )
    @patch(
        "src.workers.chat_worker.services.message_processing_service.MessageProcessingService._execute_agent_workflow"
    )
    @patch(
        "src.workers.chat_worker.services.message_processing_service.MessageProcessingService._load_job_data"
    )
    def test_happy_path_load_execute_save(
        self, mock_load, mock_execute, mock_save, mock_publish, service
    ):
        """Phase 1→2→3 completes successfully, returns True."""
        mock_load.return_value = _make_conversation_data()
        mock_execute.return_value = _make_result()

        result = service.process(uuid4())
        assert result is True
        mock_load.assert_called_once()
        mock_execute.assert_called_once()
        mock_save.assert_called_once()

    @patch(
        "src.workers.chat_worker.services.message_processing_service.MessageProcessingService._mark_job_failed"
    )
    @patch(
        "src.workers.chat_worker.services.message_processing_service.MessageProcessingService._execute_agent_workflow"
    )
    @patch(
        "src.workers.chat_worker.services.message_processing_service.MessageProcessingService._load_job_data"
    )
    def test_load_failure_returns_false(self, mock_load, mock_execute, mock_fail, service):
        """load returns None → returns False, skips execute and save."""
        mock_load.return_value = None

        result = service.process(uuid4())
        assert result is False
        mock_execute.assert_not_called()

    @patch(
        "src.workers.chat_worker.services.message_processing_service.MessageProcessingService._mark_job_failed"
    )
    @patch(
        "src.workers.chat_worker.services.message_processing_service.MessageProcessingService._publish_failure_frames"
    )
    @patch(
        "src.workers.chat_worker.services.message_processing_service.MessageProcessingService._save_results"
    )
    @patch(
        "src.workers.chat_worker.services.message_processing_service.MessageProcessingService._execute_agent_workflow"
    )
    @patch(
        "src.workers.chat_worker.services.message_processing_service.MessageProcessingService._load_job_data"
    )
    def test_agent_failure_marks_job_failed(
        self,
        mock_load,
        mock_execute,
        mock_save,
        mock_publish_failure,
        mock_fail,
        service,
    ):
        """Agent exception → _mark_job_failed called, returns False."""
        job_id = uuid4()
        mock_load.return_value = _make_conversation_data()
        mock_execute.side_effect = RuntimeError("Agent crashed")

        result = service.process(job_id)
        assert result is False
        mock_fail.assert_called_once_with(job_id, "Agent crashed")
        mock_publish_failure.assert_called_once_with(job_id, "Agent crashed")


# =============================================================================
# 6. RedisPublisher
# =============================================================================


class TestRedisPublisher:
    @patch("src.workers.chat_worker.services.message_processing_service.xadd_json_frame")
    def test_publish_calls_xadd(self, mock_xadd):
        """Publish delegates to xadd_json_frame."""
        mock_xadd.return_value = "stream-id"
        pub = RedisPublisher(uuid4(), uuid4())
        pub.publish({"type": "test", "data": "value"})
        mock_xadd.assert_called_once()

    @patch("src.workers.chat_worker.services.message_processing_service.xadd_json_frame")
    def test_publish_warns_on_failure(self, mock_xadd):
        """xadd returning None triggers a warning (not crash)."""
        mock_xadd.return_value = None
        pub = RedisPublisher(uuid4(), uuid4())
        # Should not raise
        pub.publish({"type": "test"})
