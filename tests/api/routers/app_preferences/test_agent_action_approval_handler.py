from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write


class _FakeResult:
    def __init__(self, user: SimpleNamespace | None) -> None:
        self.user = user

    def scalar_one_or_none(self) -> SimpleNamespace | None:
        return self.user


class _FakeMasterSession:
    def __init__(self, user: SimpleNamespace | None) -> None:
        self.user = user
        self.committed = False
        self.refreshed_user: SimpleNamespace | None = None

    async def execute(self, _statement):
        return _FakeResult(self.user)

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, user: SimpleNamespace) -> None:
        self.refreshed_user = user


@pytest.fixture
def action_approval_dependency_overrides(client: TestClient, test_user_ids):
    user_ids = UserIDs(
        user_id=test_user_ids["user_id"],
        organisation_id=test_user_ids["organisation_id"],
        user_name="Test User",
        user_email="test@example.com",
    )
    user = SimpleNamespace(
        user_id=user_ids.user_id,
        organisation_id=user_ids.organisation_id,
        preferences={},
    )
    session = _FakeMasterSession(user)

    async def _override_user():
        return user_ids

    async def _override_master_session():
        yield session

    async def _override_rate_limit():
        return None

    from src.infra.utils.app_state import fastapp as app

    previous_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_master_async_session] = _override_master_session
    app.dependency_overrides[rate_limit_user_read] = _override_rate_limit
    app.dependency_overrides[rate_limit_user_write] = _override_rate_limit
    yield SimpleNamespace(user_ids=user_ids, user=user, session=session)
    app.dependency_overrides = previous_overrides


def test_get_agent_action_approval_preferences_returns_defaults(
    client: TestClient, action_approval_dependency_overrides
):
    response = client.get("/users/preference/agent-actions")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "SUCCESS"

    tools = {item["tool_name"]: item for item in body["data"]["tools"]}
    assert tools["create_task"]["approval_required"] is False
    assert tools["create_task"]["editable"] is True
    assert tools["send_email"]["approval_required"] is True
    assert tools["send_email"]["editable"] is False


def test_patch_agent_action_approval_preferences_persists_user_overrides(
    client: TestClient, action_approval_dependency_overrides
):
    response = client.patch(
        "/users/preference/agent-actions",
        json={
            "tools": {
                "create_task": True,
                "send_email": False,
                "unknown_tool": False,
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    tools = {item["tool_name"]: item for item in body["data"]["tools"]}

    assert tools["create_task"]["approval_required"] is True
    assert tools["send_email"]["approval_required"] is True
    assert "unknown_tool" not in tools
    assert action_approval_dependency_overrides.session.committed is True
    assert action_approval_dependency_overrides.session.refreshed_user is (
        action_approval_dependency_overrides.user
    )
    assert (
        action_approval_dependency_overrides.user.preferences["agent_action_approvals"]["tools"][
            "create_task"
        ]["approval_required"]
        is True
    )
    assert (
        action_approval_dependency_overrides.user.preferences["agent_action_approvals"]["tools"][
            "send_email"
        ]["approval_required"]
        is True
    )
