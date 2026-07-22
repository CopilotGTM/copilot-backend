from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.dependencies.rate_limit import rate_limit_user_write


class _FakeResult:
    def __init__(self, user: SimpleNamespace) -> None:
        self.user = user

    def scalar_one_or_none(self) -> SimpleNamespace:
        return self.user


class _FakeMasterSession:
    def __init__(self, user: SimpleNamespace) -> None:
        self.user = user
        self.committed = False

    async def execute(self, _statement):
        return _FakeResult(self.user)

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _user: SimpleNamespace) -> None:
        return None


@pytest.fixture
def navigation_dependency_overrides(client: TestClient, test_user_ids):
    user_ids = UserIDs(
        user_id=test_user_ids["user_id"],
        organisation_id=test_user_ids["organisation_id"],
        user_name="Test User",
        user_email="test@example.com",
    )
    user = SimpleNamespace(
        user_id=user_ids.user_id,
        organisation_id=user_ids.organisation_id,
        preferences={"crm": {"deals": {"preferred_currency": "INR"}}},
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
    app.dependency_overrides[rate_limit_user_write] = _override_rate_limit
    yield SimpleNamespace(user=user, session=session)
    app.dependency_overrides = previous_overrides


def test_patch_navigation_preferences_persists_owned_branch(
    client: TestClient, navigation_dependency_overrides
) -> None:
    response = client.patch(
        "/users/preference/navigation",
        json={
            "expanded_sections": {
                "workspace": False,
                "records": True,
                "analytics": True,
                "settings_workspace_setup": True,
                "settings_data": False,
                "settings_automation": True,
                "settings_library": True,
                "settings_account": True,
                "settings_crm_fields": True,
            },
            "last_settings_item": "crm_fields",
            "crm_fields_entity": "lead",
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["expanded_sections"]["workspace"] is False
    assert data["last_settings_item"] == "crm_fields"
    assert data["crm_fields_entity"] == "lead"
    assert navigation_dependency_overrides.session.committed is True
    assert (
        navigation_dependency_overrides.user.preferences["crm"]["deals"]["preferred_currency"]
        == "INR"
    )


def test_patch_navigation_preferences_rejects_unknown_settings_item(
    client: TestClient, navigation_dependency_overrides
) -> None:
    response = client.patch(
        "/users/preference/navigation",
        json={"last_settings_item": "unknown"},
    )

    assert response.status_code == 422
    assert navigation_dependency_overrides.session.committed is False
