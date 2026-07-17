"""HTTP contracts for owner-managed read-only chat sharing."""

from uuid import UUID, uuid4

from fastapi.testclient import TestClient


def _create_chat(client: TestClient) -> str:
    response = client.post("/users/chats/create_chat", json={"title": "Shared chat"})
    assert response.status_code == 200, response.text
    return response.json()["data"]["session_id"]


def test_share_lifecycle_and_duplicate_put(
    client: TestClient,
    dependency_overrides,
) -> None:
    """PUT is idempotent, GET lists the grant, and DELETE returns empty 204."""
    session_id = _create_chat(client)
    viewer_id = uuid4()
    share_url = f"/users/chats/{session_id}/shares/{viewer_id}"

    first = client.put(share_url)
    duplicate = client.put(share_url)
    listed = client.get(f"/users/chats/{session_id}/shares")

    assert first.status_code == 200, first.text
    assert duplicate.status_code == 200, duplicate.text
    assert first.json()["data"]["viewer_user_id"] == str(viewer_id)
    assert duplicate.json()["data"] == first.json()["data"]
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"] == [first.json()["data"]]

    revoked = client.delete(share_url)
    assert revoked.status_code == 204
    assert revoked.content == b""

    after_revoke = client.get(f"/users/chats/{session_id}/shares")
    assert after_revoke.status_code == 200
    assert after_revoke.json()["data"] == []


def test_owner_cannot_be_added_as_viewer(
    client: TestClient,
    dependency_overrides,
    test_user_ids,
) -> None:
    """Owner-self validation is a client error rather than a hidden not-found."""
    session_id = _create_chat(client)

    response = client.put(f"/users/chats/{session_id}/shares/{test_user_ids['user_id']}")

    assert response.status_code == 400, response.text
    assert "owner already has access" in response.json()["message"].lower()


def test_inaccessible_session_uses_non_enumerating_not_found(
    client: TestClient,
    dependency_overrides,
) -> None:
    """Share management must not reveal whether another owner's chat exists."""
    response = client.put(f"/users/chats/{uuid4()}/shares/{uuid4()}")

    assert response.status_code == 404, response.text
    assert response.json()["message"] == "Chat session or viewer not found"


def test_invalid_viewer_uses_same_not_found_message(
    client: TestClient,
    dependency_overrides,
) -> None:
    """An invalid viewer is indistinguishable from an inaccessible session."""
    session_id = _create_chat(client)

    response = client.put(f"/users/chats/{session_id}/shares/{UUID(int=0)}")

    assert response.status_code == 404, response.text
    assert response.json()["message"] == "Chat session or viewer not found"


def test_list_and_revoke_hide_inaccessible_session(
    client: TestClient,
    dependency_overrides,
) -> None:
    """GET and DELETE use the same non-enumerating owner boundary as PUT."""
    session_id = uuid4()
    viewer_id = uuid4()

    listed = client.get(f"/users/chats/{session_id}/shares")
    revoked = client.delete(f"/users/chats/{session_id}/shares/{viewer_id}")

    assert listed.status_code == 404
    assert revoked.status_code == 404
    assert listed.json()["message"] == "Chat session or viewer not found"
    assert revoked.json()["message"] == "Chat session or viewer not found"


def test_main_chat_list_combines_owned_and_shared_by_recency(
    client: TestClient,
    dependency_overrides,
) -> None:
    """The sidebar receives one ordered page with capability-aware metadata."""
    owned = dependency_overrides.create_owned_session()
    shared = dependency_overrides.create_shared_session()

    response = client.get("/users/chats/list_chats")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [item["session_id"] for item in data["items"]] == [
        str(shared.session_id),
        str(owned.session_id),
    ]
    assert data["items"][0]["session_id"] == str(shared.session_id)
    assert data["items"][0]["access"] == "viewer"
    assert data["items"][0]["capabilities"] == {
        "read": True,
        "send": False,
        "rename": False,
        "delete": False,
        "manage_shares": False,
    }
    assert data["items"][1]["access"] == "owner"


def test_main_chat_list_scope_filters_owned_and_shared(
    client: TestClient,
    dependency_overrides,
) -> None:
    """Tabs filter the same endpoint instead of maintaining a second collection."""
    owned = dependency_overrides.create_owned_session()
    shared = dependency_overrides.create_shared_session()

    owned_response = client.get("/users/chats/list_chats", params={"scope": "owned"})
    shared_response = client.get("/users/chats/list_chats", params={"scope": "shared"})

    assert owned_response.status_code == 200
    assert shared_response.status_code == 200
    assert [item["session_id"] for item in owned_response.json()["data"]["items"]] == [
        str(owned.session_id)
    ]
    assert [item["session_id"] for item in shared_response.json()["data"]["items"]] == [
        str(shared.session_id)
    ]


def test_shared_settings_list_includes_sharer_metadata(
    client: TestClient,
    dependency_overrides,
) -> None:
    """The settings projection identifies who shared each read-only chat."""
    shared = dependency_overrides.create_shared_session()

    response = client.get("/users/chats/shared")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["cursor"] is None
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["session_id"] == str(shared.session_id)
    assert item["access"] == "viewer"
    assert item["capabilities"]["send"] is False
    assert item["shared_at"] == "2024-01-02T00:00:00"
    assert item["shared_by"] == {
        "user_id": str(shared.user_id),
        "name": "Avery Owner",
        "email": "avery@example.com",
    }
