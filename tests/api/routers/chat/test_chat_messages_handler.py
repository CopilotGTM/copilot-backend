from uuid import uuid4

from fastapi.testclient import TestClient

from tests.api.routers.chat.conftest import FakeChatMessageService

# =============================================================================
# POST /send_message Tests
# =============================================================================


def test_send_message_success(client: TestClient, messages_dependency_overrides, mocker):
    """Test sending a message successfully."""
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


def test_send_message_celery_task_failure(
    client: TestClient, messages_dependency_overrides, mocker
):
    """Test that celery task failure returns error."""
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

    # List messages
    resp = client.get("/users/chats/messages/list", params={"session_id": session_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    data = body["data"]
    # We created 3 user messages + 3 assistant placeholders = 6 messages
    assert len(data["items"]) == 6


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
