from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write


class _FakeMasterSession:
    def __init__(self, organisation: SimpleNamespace | None) -> None:
        self.organisation = organisation
        self.committed = False
        self.refreshed = False

    async def get(self, _model, _organisation_id):
        return self.organisation

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _organisation: SimpleNamespace) -> None:
        self.refreshed = True


@pytest.fixture
def meeting_recording_policy_overrides(client: TestClient, test_user_ids):
    user_ids = UserIDs(
        user_id=test_user_ids["user_id"],
        organisation_id=test_user_ids["organisation_id"],
        user_name="Test User",
        user_email="test@example.com",
    )
    organisation = SimpleNamespace(
        organisation_id=user_ids.organisation_id,
        preferences={},
    )
    session = _FakeMasterSession(organisation)

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
    yield SimpleNamespace(user_ids=user_ids, organisation=organisation, session=session)
    app.dependency_overrides = previous_overrides


def test_get_meeting_recording_policy_returns_org_wide_default_for_tenant_user(
    client: TestClient,
    meeting_recording_policy_overrides,
):
    response = client.get("/users/preference/meeting-recording")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "SUCCESS"
    assert body["data"] == {
        "bot_join_policy": "external_only",
        "bot_join_policy_options": [
            {"value": "external_only", "label": "External meetings only"},
            {"value": "all", "label": "All meetings"},
            {"value": "never", "label": "Never join"},
        ],
    }


def test_patch_meeting_recording_policy_updates_org_wide_preference_for_tenant_user(
    client: TestClient,
    meeting_recording_policy_overrides,
):
    response = client.patch(
        "/users/preference/meeting-recording",
        json={"bot_join_policy": "all"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["bot_join_policy"] == "all"
    assert meeting_recording_policy_overrides.session.committed is True
    assert meeting_recording_policy_overrides.session.refreshed is True
    assert meeting_recording_policy_overrides.organisation.preferences["meeting_recording"] == {
        "bot_join_policy": "all",
    }
