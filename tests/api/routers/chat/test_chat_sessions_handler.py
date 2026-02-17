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


def test_delete_chat_success(client: TestClient, dependency_overrides):
    # Create a chat first
    create = client.post("/users/chats/create_chat", json={"title": "Temp"})
    assert create.status_code == 200, create.text
    session_id = create.json()["data"]["session_id"]

    # Delete it
    delete = client.request("DELETE", "/users/chats/delete_chat", json={"session_id": session_id})
    assert delete.status_code == 200, delete.text
    body = delete.json()
    assert body["status"] == "SUCCESS"
    assert body["message"] == "Chat deleted"
    assert body["data"]["session_id"] == session_id


def test_delete_chat_not_found(client: TestClient, dependency_overrides):
    # Attempt to delete a random/unknown session
    from uuid import uuid4

    resp = client.request("DELETE", "/users/chats/delete_chat", json={"session_id": str(uuid4())})
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "not found" in body["message"].lower()


def test_rename_chat_success(client: TestClient, dependency_overrides):
    # Create a chat first
    create = client.post("/users/chats/create_chat", json={"title": "Old"})
    assert create.status_code == 200, create.text
    session_id = create.json()["data"]["session_id"]

    # Rename it
    resp = client.request(
        "PATCH",
        "/users/chats/rename_chat",
        json={"session_id": session_id, "title": "New Title"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["message"] == "Chat renamed"
    assert body["data"]["title"] == "New Title"


def test_rename_chat_rejects_empty_title(client: TestClient, dependency_overrides):
    # Create a chat first
    create = client.post("/users/chats/create_chat", json={"title": "Old"})
    session_id = create.json()["data"]["session_id"]

    for bad in ["", "   "]:
        resp = client.request(
            "PATCH",
            "/users/chats/rename_chat",
            json={"session_id": session_id, "title": bad},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["status"] == "ERROR"
        assert "Title cannot be" in body["message"]


def test_rename_chat_not_found(client: TestClient, dependency_overrides):
    from uuid import uuid4

    resp = client.request(
        "PATCH",
        "/users/chats/rename_chat",
        json={"session_id": str(uuid4()), "title": "New"},
    )
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "not found" in body["message"].lower()


def test_list_chats_pagination(client: TestClient, dependency_overrides):
    # Create three chats
    titles = ["A", "B", "C"]
    for t in titles:
        r = client.post("/users/chats/create_chat", json={"title": t})
        assert r.status_code == 200

    # Page 1 with limit 2
    r1 = client.get("/users/chats/list_chats", params={"limit": 2})
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert b1["status"] == "SUCCESS"
    data1 = b1["data"]
    assert len(data1["items"]) == 2
    assert data1["cursor"] is not None

    # Page 2 with cursor
    cursor = data1["cursor"]
    r2 = client.get("/users/chats/list_chats", params={"limit": 2, "cursor": cursor})
    assert r2.status_code == 200
    b2 = r2.json()
    data2 = b2["data"]
    assert len(data2["items"]) == 1


def test_search_chats_typeahead(client: TestClient, dependency_overrides):
    # Seed distinct titles
    titles = ["Alpha", "Beta", "Gamma", "Delta"]
    for t in titles:
        client.post("/users/chats/create_chat", json={"title": t})

    # Search for 'al' -> Alpha only
    r1 = client.get("/users/chats/search_chats", params={"q": "al", "limit": 5})
    assert r1.status_code == 200, r1.text
    names1 = [i["title"] for i in r1.json()["data"]["items"]]
    assert any(n == "Alpha" for n in names1)

    # Search for 'ga' -> Gamma only
    r2 = client.get("/users/chats/search_chats", params={"q": "ga"})
    assert r2.status_code == 200
    names2 = [i["title"] for i in r2.json()["data"]["items"]]
    assert any(n == "Gamma" for n in names2)


def test_favorite_chat_toggle(client: TestClient, dependency_overrides):
    # Create a chat
    create = client.post("/users/chats/create_chat", json={"title": "Fav Me"})
    assert create.status_code == 200, create.text
    session_id = create.json()["data"]["session_id"]

    # Mark favorite
    fav = client.request(
        "PATCH",
        "/users/chats/favorite_chat",
        json={"session_id": session_id, "favorite": True},
    )
    assert fav.status_code == 200, fav.text
    body = fav.json()
    assert body["status"] == "SUCCESS"
    assert body["message"] == "Chat favorited"
    assert body["data"]["is_favorite"] is True

    # Unmark favorite
    unfav = client.request(
        "PATCH",
        "/users/chats/favorite_chat",
        json={"session_id": session_id, "favorite": False},
    )
    assert unfav.status_code == 200, unfav.text
    body2 = unfav.json()
    assert body2["status"] == "SUCCESS"
    assert body2["message"] == "Chat unfavorited"
    assert body2["data"]["is_favorite"] is False


def test_create_chat_title_just_under_boundary(client: TestClient, dependency_overrides):
    title = "A" * 99
    resp = client.post("/users/chats/create_chat", json={"title": title})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["title"] == title


def test_create_chat_title_at_boundary(client: TestClient, dependency_overrides):
    title = "A" * 100
    resp = client.post("/users/chats/create_chat", json={"title": title})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["title"] == title


def test_create_chat_truncates_long_title(client: TestClient, dependency_overrides):
    title = "A" * 150
    resp = client.post("/users/chats/create_chat", json={"title": title})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data["title"]) <= 100
    assert data["title"].endswith("...")
    assert data["title"] == "A" * 97 + "..."


def test_create_chat_long_message_as_title(client: TestClient, dependency_overrides):
    title = (
        "I wanted to discuss the quarterly sales projections and how they align "
        "with our current pipeline metrics and the forecasted revenue targets "
        "for the upcoming fiscal year"
    )
    assert len(title) > 100
    resp = client.post("/users/chats/create_chat", json={"title": title})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data["title"]) <= 100
    assert data["title"].endswith("...")


def test_rename_chat_truncates_long_title(client: TestClient, dependency_overrides):
    create = client.post("/users/chats/create_chat", json={"title": "Short"})
    assert create.status_code == 200, create.text
    session_id = create.json()["data"]["session_id"]

    long_title = "B" * 120
    resp = client.request(
        "PATCH",
        "/users/chats/rename_chat",
        json={"session_id": session_id, "title": long_title},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data["title"]) <= 100
    assert data["title"].endswith("...")
    assert data["title"] == "B" * 97 + "..."


def test_delete_already_deleted_chat(client: TestClient, dependency_overrides):
    # Create a chat, delete it, then try to delete again
    create = client.post("/users/chats/create_chat", json={"title": "Doomed"})
    assert create.status_code == 200, create.text
    session_id = create.json()["data"]["session_id"]

    first_del = client.request(
        "DELETE", "/users/chats/delete_chat", json={"session_id": session_id}
    )
    assert first_del.status_code == 200

    second_del = client.request(
        "DELETE", "/users/chats/delete_chat", json={"session_id": session_id}
    )
    assert second_del.status_code == 404, second_del.text
    assert "not found" in second_del.json()["message"].lower()


def test_list_chats_empty(client: TestClient, dependency_overrides):
    resp = client.get("/users/chats/list_chats")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    data = body["data"]
    assert data["items"] == []
    assert data["cursor"] is None


def test_search_chats_no_results(client: TestClient, dependency_overrides):
    client.post("/users/chats/create_chat", json={"title": "Alpha"})
    resp = client.get("/users/chats/search_chats", params={"q": "zzzznotfound"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["items"] == []


def test_favorite_chat_not_found(client: TestClient, dependency_overrides):
    from uuid import uuid4

    resp = client.request(
        "PATCH",
        "/users/chats/favorite_chat",
        json={"session_id": str(uuid4()), "favorite": True},
    )
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "not found" in body["message"].lower()
