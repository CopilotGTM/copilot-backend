from fastapi.testclient import TestClient


def test_create_chat_minimal(client: TestClient, dependency_overrides):
    payload = {"title": "My First Chat"}
    resp = client.post("/users/chats/create_chat", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["message"] == "Chat created"
    data = body["data"]
    assert data["title"] == payload["title"]
    assert data["chat_type"] in ("GENERAL_CHAT", "ChatType.GENERAL_CHAT")
    assert "session_id" in data


def test_create_chat_with_tags(client: TestClient, dependency_overrides):
    payload = {
        "title": "Tech Deck Session",
        "tags": ["deck", "alpha"],
    }
    resp = client.post("/users/chats/create_chat", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert data["title"] == payload["title"]
    # chat_type is not accepted as input for now; default is GENERAL_CHAT
    assert data["chat_type"] == "GENERAL_CHAT"
    assert data["tags"] == payload["tags"]


def test_create_chat_rejects_empty_title(client: TestClient, dependency_overrides):
    for bad in ["", "   "]:
        resp = client.post("/users/chats/create_chat", json={"title": bad})
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["status"] == "ERROR"
        assert "Title cannot be" in body["message"]
