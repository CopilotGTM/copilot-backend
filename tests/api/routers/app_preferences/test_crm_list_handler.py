"""API contracts for user-owned CRM collection sorting."""

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
def crm_list_dependency_overrides(client: TestClient, test_user_ids):
    user_ids = UserIDs(
        user_id=test_user_ids["user_id"],
        organisation_id=test_user_ids["organisation_id"],
        user_name="Test User",
        user_email="test@example.com",
    )
    user = SimpleNamespace(
        user_id=user_ids.user_id,
        organisation_id=user_ids.organisation_id,
        preferences={
            "crm": {
                "accounts": {"list": {"columns": ["name", "owner.name"]}},
                "deals": {"preferred_currency": "INR"},
            }
        },
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


@pytest.mark.parametrize(
    ("entity", "sort_by", "sort_order"),
    [
        ("contacts", "last_activity_at", "desc"),
        ("accounts", "created_at", "desc"),
        ("deals", "amount", "asc"),
        ("tasks", "due_date", "desc"),
    ],
)
def test_patch_crm_list_sorting_persists_each_supported_collection(
    client: TestClient,
    crm_list_dependency_overrides,
    entity: str,
    sort_by: str,
    sort_order: str,
) -> None:
    """Each sortable CRM collection should save its own independent selection."""
    response = client.patch(
        f"/users/preference/crm/lists/{entity}/sorting",
        json={"sort_by": sort_by, "sort_order": sort_order},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"sort_by": sort_by, "sort_order": sort_order}
    assert (
        crm_list_dependency_overrides.user.preferences["crm"][entity]["list"]["sort_by"] == sort_by
    )
    assert crm_list_dependency_overrides.session.committed is True
    assert (
        crm_list_dependency_overrides.user.preferences["crm"]["deals"]["preferred_currency"]
        == "INR"
    )


def test_patch_crm_list_sorting_rejects_an_unsupported_field(
    client: TestClient,
    crm_list_dependency_overrides,
) -> None:
    """A list must not persist a sort field its read API cannot execute."""
    response = client.patch(
        "/users/preference/crm/lists/accounts/sorting",
        json={"sort_by": "pipeline_value", "sort_order": "desc"},
    )

    assert response.status_code == 422
    assert crm_list_dependency_overrides.session.committed is False


def test_patch_crm_list_sorting_preserves_existing_columns(
    client: TestClient,
    crm_list_dependency_overrides,
) -> None:
    """Saving sorting should merge into rather than replace the existing list branch."""
    response = client.patch(
        "/users/preference/crm/lists/accounts/sorting",
        json={"sort_by": "owner", "sort_order": "asc"},
    )

    assert response.status_code == 200
    assert crm_list_dependency_overrides.user.preferences["crm"]["accounts"]["list"]["columns"] == [
        "name",
        "owner.name",
    ]
