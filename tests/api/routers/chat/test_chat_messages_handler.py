"""API contracts for recoverable web-chat publication, history, and feedback."""

from uuid import uuid4

from fastapi.testclient import TestClient
from src.api.routers.chat import messages_handler
from src.domain.models.organisation_tables.job_models import JobStatus

from tests.api.routers.chat.conftest import FakeChatMessageService


def test_message_events_rejects_job_not_owned_by_user(
    client: TestClient, messages_dependency_overrides, monkeypatch
) -> None:
    """Reject an unowned job before opening a transient event stream."""
    closed = False

    class FakeSSEService:
        """Expose a denied stream while recording resource cleanup."""

        def __init__(self, *_args):
            pass

        async def can_stream(self):
            """Model a job that is not owned by the authenticated user."""
            return False

        async def close(self):
            """Record that rejection closes the service before responding."""
            nonlocal closed
            closed = True

        async def stream_events(self, **_kwargs):
            """Keep the fake valid if a regression reaches stream creation."""
            yield {"event": "complete", "data": "{}"}

    monkeypatch.setattr(messages_handler, "ChatMessageSSEService", FakeSSEService)

    response = client.get(
        "/users/chats/message_events",
        params={"job_id": str(uuid4())},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Chat job not found"
    assert closed is True


# =============================================================================
# POST /send_message Tests
# =============================================================================


def test_send_message_success(client: TestClient, messages_dependency_overrides, mocker):
    """Test sending a message successfully."""
    mocker.patch("src.api.dependencies.rate_limit._enforce_rate_limit")
    # Mock the celery task to prevent actual task dispatch
    mocker.patch(
        "src.api.routers.chat.messages_handler.send_chat_task_async",
        return_value="mock-task-id",
    )

    session_id = str(uuid4())
    payload = {
        "session_id": session_id,
        "content": "Hello, how are you?",
    }
    resp = client.post("/users/chats/send_message", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["message"] == "Message accepted"
    data = body["data"]
    assert "job_id" in data
    assert "user_message_id" in data
    assert "assistant_message_id" in data
    job = messages_dependency_overrides.jobs[data["job_id"]]
    assert job.status == JobStatus.PENDING
    assert job.job_metadata["enqueue_status"] == "enqueued"
    assert job.job_metadata["enqueue_attempts"] == 1
    assert job.job_metadata["celery_task_id"] == "mock-task-id"


def test_send_message_with_message_type(client: TestClient, messages_dependency_overrides, mocker):
    """Test sending a message with a specific message type."""
    mocker.patch(
        "src.api.routers.chat.messages_handler.send_chat_task_async",
        return_value="mock-task-id",
    )

    session_id = str(uuid4())
    payload = {
        "session_id": session_id,
        "content": "Create a technical deck",
        "message_type": "TECHNICAL_DECK",
    }
    resp = client.post("/users/chats/send_message", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"


def test_send_message_uses_context_user_timezone_for_service(
    client: TestClient, messages_dependency_overrides, mocker
):
    """Test sending a message uses the authenticated user's timezone context."""
    mocker.patch(
        "src.api.routers.chat.messages_handler.send_chat_task_async",
        return_value="mock-task-id",
    )

    session_id = str(uuid4())
    payload = {
        "session_id": session_id,
        "content": "What meetings do I have tomorrow?",
    }
    resp = client.post("/users/chats/send_message", json=payload)
    assert resp.status_code == 200, resp.text
    assert messages_dependency_overrides.last_create_user_message_call is not None
    assert (
        messages_dependency_overrides.last_create_user_message_call["user_timezone"]
        == "Asia/Kolkata"
    )


def test_send_message_empty_content_rejected(client: TestClient, messages_dependency_overrides):
    """Test that empty content is rejected."""
    session_id = str(uuid4())
    for bad_content in ["", "   ", "\n\t"]:
        payload = {
            "session_id": session_id,
            "content": bad_content,
        }
        resp = client.post("/users/chats/send_message", json=payload)
        assert resp.status_code == 422, (
            f"Expected 422 for content='{bad_content}', got {resp.status_code}"
        )


def test_send_message_invalid_role_rejected(client: TestClient, messages_dependency_overrides):
    """Test that non-USER roles are rejected."""
    session_id = str(uuid4())
    payload = {
        "session_id": session_id,
        "content": "Hello",
        "role": "assistant",  # Invalid - only USER is allowed
    }
    resp = client.post("/users/chats/send_message", json=payload)
    assert resp.status_code == 422, resp.text


def test_send_message_is_edited_without_edited_message_id_rejected(
    client: TestClient, messages_dependency_overrides
):
    """Test that is_edited=True requires edited_message_id."""
    session_id = str(uuid4())
    payload = {
        "session_id": session_id,
        "content": "Edited content",
        "is_edited": True,
        # Missing edited_message_id
    }
    resp = client.post("/users/chats/send_message", json=payload)
    assert resp.status_code == 422, resp.text


def test_send_message_edited_with_edited_message_id(
    client: TestClient, messages_dependency_overrides, mocker
):
    """Test that is_edited=True with a valid edited_message_id is accepted."""
    mocker.patch(
        "src.api.routers.chat.messages_handler.send_chat_task_async",
        return_value="mock-task-id",
    )

    session_id = str(uuid4())
    payload = {
        "session_id": session_id,
        "content": "Updated content",
        "is_edited": True,
        "edited_message_id": str(uuid4()),
    }
    resp = client.post("/users/chats/send_message", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["message"] == "Message accepted"


def test_send_message_with_attachments(client: TestClient, messages_dependency_overrides, mocker):
    """Test sending a message with attachments."""
    mocker.patch(
        "src.api.routers.chat.messages_handler.send_chat_task_async",
        return_value="mock-task-id",
    )

    session_id = str(uuid4())
    attachment_ids = [str(uuid4()), str(uuid4())]
    payload = {
        "session_id": session_id,
        "content": "Here are the files",
        "attachments": attachment_ids,
    }
    resp = client.post("/users/chats/send_message", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"


def test_send_message_celery_task_failure_is_terminal(
    client: TestClient, messages_dependency_overrides, mocker
):
    """Instant web chat fails immediately when Redis does not accept its task."""
    mocker.patch("src.api.dependencies.rate_limit._enforce_rate_limit")
    mocker.patch(
        "src.api.routers.chat.messages_handler.send_chat_task_async",
        side_effect=Exception("Celery connection failed"),
    )

    session_id = str(uuid4())
    payload = {
        "session_id": session_id,
        "content": "Hello",
    }
    resp = client.post("/users/chats/send_message", json=payload)
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    job = next(iter(messages_dependency_overrides.jobs.values()))
    assert job.status == JobStatus.FAILED
    assert job.job_metadata["enqueue_status"] == "failed"
    assert job.job_metadata["enqueue_attempts"] == 1
    assert "Celery connection failed" in job.job_metadata["last_enqueue_error"]


# =============================================================================
# GET /messages/list Tests
# =============================================================================


def test_list_messages_empty_session(client: TestClient, messages_dependency_overrides):
    """Test listing messages for a session with no messages."""
    session_id = str(uuid4())
    resp = client.get("/users/chats/messages/list", params={"session_id": session_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["message"] == "Messages listed"
    data = body["data"]
    assert data["items"] == []
    assert data["cursor"] is None


def test_list_messages_includes_active_jobs_for_refresh(
    client: TestClient,
    messages_dependency_overrides: FakeChatMessageService,
    mocker,
):
    """A refreshed chat can reconstruct every active SSE subscription."""
    mocker.patch(
        "src.api.routers.chat.messages_handler.send_chat_task_async",
        return_value="mock-task-id",
    )
    session_id = str(uuid4())
    sent = client.post(
        "/users/chats/send_message",
        json={"session_id": session_id, "content": "Write a long answer"},
    )
    assert sent.status_code == 200

    response = client.get(
        "/users/chats/messages/list",
        params={"session_id": session_id},
    )

    assert response.status_code == 200
    assert response.json()["data"]["active_jobs"] == [
        {
            "job_id": sent.json()["data"]["job_id"],
            "user_message_id": sent.json()["data"]["user_message_id"],
            "assistant_message_id": sent.json()["data"]["assistant_message_id"],
            "message_type": "GENERAL_MESSAGE",
            "status": "pending",
        }
    ]


def test_list_messages_with_messages(
    client: TestClient, messages_dependency_overrides: FakeChatMessageService, mocker
):
    """Test listing messages for a session with messages."""
    mocker.patch(
        "src.api.routers.chat.messages_handler.send_chat_task_async",
        return_value="mock-task-id",
    )

    session_id = str(uuid4())

    # Create some messages first
    for i in range(3):
        payload = {
            "session_id": session_id,
            "content": f"Message {i}",
        }
        create_resp = client.post("/users/chats/send_message", json=payload)
        assert create_resp.status_code == 200

    # A direct load of a shared chat must carry viewer capabilities even when
    # the sidebar metadata was not used to navigate here.
    messages_dependency_overrides.sessions[session_id]["user_id"] = str(uuid4())

    # List messages
    resp = client.get("/users/chats/messages/list", params={"session_id": session_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    data = body["data"]
    # We created 3 user messages + 3 assistant placeholders = 6 messages
    assert len(data["items"]) == 6
    assert data["session"]["session_id"] == session_id
    assert data["session"]["access"] == "viewer"
    assert data["session"]["capabilities"] == {
        "read": True,
        "send": False,
        "rename": False,
        "delete": False,
        "manage_shares": False,
    }


def test_list_messages_pagination(
    client: TestClient, messages_dependency_overrides: FakeChatMessageService, mocker
):
    """Test pagination for listing messages."""
    mocker.patch(
        "src.api.routers.chat.messages_handler.send_chat_task_async",
        return_value="mock-task-id",
    )

    session_id = str(uuid4())

    # Create more messages than the limit
    for i in range(5):
        payload = {
            "session_id": session_id,
            "content": f"Message {i}",
        }
        create_resp = client.post("/users/chats/send_message", json=payload)
        assert create_resp.status_code == 200

    # List first page with limit 3
    resp1 = client.get(
        "/users/chats/messages/list",
        params={"session_id": session_id, "limit": 3},
    )
    assert resp1.status_code == 200, resp1.text
    body1 = resp1.json()
    data1 = body1["data"]
    assert len(data1["items"]) == 3
    assert data1["cursor"] is not None

    # List second page
    resp2 = client.get(
        "/users/chats/messages/list",
        params={"session_id": session_id, "limit": 3, "cursor": data1["cursor"]},
    )
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    data2 = body2["data"]
    assert len(data2["items"]) == 3


def test_list_messages_filter_by_role(
    client: TestClient, messages_dependency_overrides: FakeChatMessageService, mocker
):
    """Test filtering messages by role."""
    mocker.patch(
        "src.api.routers.chat.messages_handler.send_chat_task_async",
        return_value="mock-task-id",
    )

    session_id = str(uuid4())

    # Create a message
    payload = {
        "session_id": session_id,
        "content": "Hello",
    }
    create_resp = client.post("/users/chats/send_message", json=payload)
    assert create_resp.status_code == 200

    # Filter by user role
    resp = client.get(
        "/users/chats/messages/list",
        params={"session_id": session_id, "role": "user"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    # All returned messages should be user messages
    for item in data["items"]:
        assert item["role"] == "user"


def test_list_messages_filter_by_message_type(
    client: TestClient, messages_dependency_overrides: FakeChatMessageService, mocker
):
    """Test filtering messages by message type."""
    mocker.patch(
        "src.api.routers.chat.messages_handler.send_chat_task_async",
        return_value="mock-task-id",
    )

    session_id = str(uuid4())

    # Create messages with different types
    payload1 = {
        "session_id": session_id,
        "content": "General message",
        "message_type": "GENERAL_MESSAGE",
    }
    client.post("/users/chats/send_message", json=payload1)

    payload2 = {
        "session_id": session_id,
        "content": "Technical deck",
        "message_type": "TECHNICAL_DECK",
    }
    client.post("/users/chats/send_message", json=payload2)

    # Filter by TECHNICAL_DECK type
    resp = client.get(
        "/users/chats/messages/list",
        params={"session_id": session_id, "message_type": "TECHNICAL_DECK"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    # All returned messages should be TECHNICAL_DECK type
    for item in data["items"]:
        assert item["message_type"] == "TECHNICAL_DECK"


# =============================================================================
# POST /messages/feedback Tests
# =============================================================================


def test_feedback_thumbs_up(
    client: TestClient, messages_dependency_overrides: FakeChatMessageService, mocker
):
    """Test providing thumbs up feedback."""
    mocker.patch(
        "src.api.routers.chat.messages_handler.send_chat_task_async",
        return_value="mock-task-id",
    )

    session_id = str(uuid4())

    # Create a message first
    payload = {
        "session_id": session_id,
        "content": "Hello",
    }
    create_resp = client.post("/users/chats/send_message", json=payload)
    assert create_resp.status_code == 200
    message_id = create_resp.json()["data"]["user_message_id"]

    # Provide thumbs up feedback
    feedback_payload = {
        "message_id": message_id,
        "feedback_score": 1,
    }
    resp = client.post("/users/chats/messages/feedback", json=feedback_payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["message"] == "Feedback updated successfully"
    data = body["data"]
    assert data["message_id"] == message_id
    assert data["feedback_score"] == 1


def test_feedback_thumbs_down(
    client: TestClient, messages_dependency_overrides: FakeChatMessageService, mocker
):
    """Test providing thumbs down feedback."""
    mocker.patch(
        "src.api.routers.chat.messages_handler.send_chat_task_async",
        return_value="mock-task-id",
    )

    session_id = str(uuid4())

    # Create a message first
    payload = {
        "session_id": session_id,
        "content": "Hello",
    }
    create_resp = client.post("/users/chats/send_message", json=payload)
    assert create_resp.status_code == 200
    message_id = create_resp.json()["data"]["user_message_id"]

    # Provide thumbs down feedback
    feedback_payload = {
        "message_id": message_id,
        "feedback_score": -1,
    }
    resp = client.post("/users/chats/messages/feedback", json=feedback_payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    data = body["data"]
    assert data["feedback_score"] == -1


def test_feedback_clear(
    client: TestClient, messages_dependency_overrides: FakeChatMessageService, mocker
):
    """Test clearing feedback."""
    mocker.patch(
        "src.api.routers.chat.messages_handler.send_chat_task_async",
        return_value="mock-task-id",
    )

    session_id = str(uuid4())

    # Create a message first
    payload = {
        "session_id": session_id,
        "content": "Hello",
    }
    create_resp = client.post("/users/chats/send_message", json=payload)
    assert create_resp.status_code == 200
    message_id = create_resp.json()["data"]["user_message_id"]

    # First set feedback
    feedback_payload = {
        "message_id": message_id,
        "feedback_score": 1,
    }
    client.post("/users/chats/messages/feedback", json=feedback_payload)

    # Then clear it
    clear_payload = {
        "message_id": message_id,
        "feedback_score": 0,
    }
    resp = client.post("/users/chats/messages/feedback", json=clear_payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    data = body["data"]
    # Cleared feedback should be None
    assert data["feedback_score"] is None


def test_feedback_message_not_found(client: TestClient, messages_dependency_overrides):
    """Test providing feedback for a non-existent message."""
    feedback_payload = {
        "message_id": str(uuid4()),
        "feedback_score": 1,
    }
    resp = client.post("/users/chats/messages/feedback", json=feedback_payload)
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "not found" in body["message"].lower()


def test_feedback_invalid_score(client: TestClient, messages_dependency_overrides):
    """Test that invalid feedback scores are rejected."""
    message_id = str(uuid4())
    for bad_score in [2, -2, 5, -5]:
        feedback_payload = {
            "message_id": message_id,
            "feedback_score": bad_score,
        }
        resp = client.post("/users/chats/messages/feedback", json=feedback_payload)
        assert resp.status_code == 422, (
            f"Expected 422 for score={bad_score}, got {resp.status_code}"
        )
