"""API coverage for the preferred calendar sender endpoint."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_write


class _FakeResult:
    """Return one scalar value from an in-memory dependency session."""

    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        """Mirror the SQLAlchemy result method used by the preference service."""
        return self.value


class _FakeMasterSession:
    """Persist preference mutations on an in-memory user."""

    def __init__(self, user: SimpleNamespace) -> None:
        self.user = user
        self.committed = False

    async def execute(self, _statement):
        """Return the authenticated user for the tenant-scoped lookup."""
        return _FakeResult(self.user)

    async def commit(self) -> None:
        """Record that the handler reached its durable write boundary."""
        self.committed = True

    async def refresh(self, _user: SimpleNamespace) -> None:
        """Match the async session contract; no refresh is needed in memory."""
        return None


class _FakeOrgSession:
    """Expose one connected calendar integration identifier."""

    async def execute(self, _statement):
        """Return a connected integration for sender validation."""
        return _FakeResult(uuid4())


@pytest.fixture
def calendar_sender_dependency_overrides(client: TestClient, test_user_ids):
    """Replace auth and database dependencies with tenant-bound in-memory values."""
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
    master_session = _FakeMasterSession(user)
    org_session = _FakeOrgSession()

    async def _override_user():
        """Return the authenticated request identity."""
        return user_ids

    async def _override_master_session():
        """Yield the in-memory master session."""
        yield master_session

    async def _override_org_session():
        """Yield the organization session used for connection validation."""
        yield org_session

    async def _override_rate_limit():
        """Disable rate limiting in the isolated route test."""
        return None

    from src.infra.utils.app_state import fastapp as app

    previous_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_master_async_session] = _override_master_session
    app.dependency_overrides[get_org_async_session] = _override_org_session
    app.dependency_overrides[rate_limit_user_write] = _override_rate_limit
    yield SimpleNamespace(user=user, master_session=master_session)
    app.dependency_overrides = previous_overrides


def test_patch_calendar_sender_stores_connected_account(
    client: TestClient,
    calendar_sender_dependency_overrides,
) -> None:
    """The public endpoint should normalize and persist a connected calendar."""
    response = client.patch(
        "/users/preference/calendar-sender",
        json={"preferred_sender_email": "Sales@Contoso.com"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["preferred_sender_email"] == "sales@contoso.com"
    assert (
        calendar_sender_dependency_overrides.user.preferences["communications"]["calendar"][
            "preferred_sender_email"
        ]
        == "sales@contoso.com"
    )
    assert calendar_sender_dependency_overrides.master_session.committed is True
