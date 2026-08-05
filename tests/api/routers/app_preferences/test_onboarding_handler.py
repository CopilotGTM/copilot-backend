from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.dependencies.rate_limit import rate_limit_user_write
from src.domain.models.master_tables import Organisation


class _FakeResult:
    def __init__(self, user: SimpleNamespace) -> None:
        self.user = user

    def scalar_one_or_none(self) -> SimpleNamespace:
        return self.user


class _FakeMasterSession:
    def __init__(self, user: SimpleNamespace, organisation: SimpleNamespace) -> None:
        self.user = user
        self.organisation = organisation
        self.committed = False

    async def get(self, model, _identifier):
        return self.organisation if model is Organisation else None

    async def execute(self, _statement):
        return _FakeResult(self.user)

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _user: SimpleNamespace) -> None:
        return None


@pytest.fixture
def onboarding_dependency_overrides(client: TestClient, test_user_ids):
    user_ids = UserIDs(
        user_id=test_user_ids["user_id"],
        organisation_id=test_user_ids["organisation_id"],
        user_name="Test User",
        user_email="test@example.com",
    )
    user = SimpleNamespace(
        user_id=user_ids.user_id,
        organisation_id=user_ids.organisation_id,
        preferences={"notifications": {"email": True}},
    )
    organisation = SimpleNamespace(
        preferences={
            "onboarding": {
                "enabled": True,
                "steps": [
                    {"id": "google", "skippable": False},
                    {"id": "whatsapp", "skippable": True},
                ],
            }
        }
    )
    session = _FakeMasterSession(user, organisation)

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


def test_skip_optional_onboarding_step_persists_user_progress(
    client: TestClient, onboarding_dependency_overrides
) -> None:
    response = client.patch("/users/preference/onboarding/whatsapp/skip")

    assert response.status_code == 200
    assert response.json()["data"] == {"skipped_steps": ["whatsapp"]}
    assert onboarding_dependency_overrides.session.committed is True
    assert onboarding_dependency_overrides.user.preferences == {
        "notifications": {"email": True},
        "onboarding": {"skipped_steps": ["whatsapp"]},
    }


def test_skip_required_onboarding_step_is_rejected(
    client: TestClient, onboarding_dependency_overrides
) -> None:
    response = client.patch("/users/preference/onboarding/google/skip")

    assert response.status_code == 409
    assert onboarding_dependency_overrides.session.committed is False
    assert "onboarding" not in onboarding_dependency_overrides.user.preferences
