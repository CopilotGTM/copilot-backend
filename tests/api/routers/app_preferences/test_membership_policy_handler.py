"""Only organisation admins can read or update domain admission policy."""

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.api.routers.app_preferences import membership_handler as module
from src.domain.models.master_tables import UserAppRole


def _client(role, monkeypatch):
    user = SimpleNamespace(organisation_id=uuid4(), user_role=role)

    class _Service:
        def __init__(self, _session, _organisation_id):
            pass

        async def get_policy(self):
            return {"domain_join_policy": "open"}

        async def update_policy(self, request):
            return {"domain_join_policy": request.domain_join_policy}

    async def master_session():
        yield object()

    monkeypatch.setattr(module, "MembershipPolicyService", _Service)
    app = FastAPI()
    app.include_router(module.membership_policy_router, prefix="/users/preference")
    app.dependency_overrides[module.user_auth] = lambda: user
    app.dependency_overrides[module.get_master_async_session] = master_session
    app.dependency_overrides[module.rate_limit_user_read] = lambda: None
    app.dependency_overrides[module.rate_limit_user_write] = lambda: None
    return TestClient(app)


def test_member_cannot_read_or_change_membership_policy(monkeypatch) -> None:
    client = _client(UserAppRole.MEMBER, monkeypatch)

    assert client.get("/users/preference/membership").status_code == 403
    assert (
        client.patch(
            "/users/preference/membership",
            json={"domain_join_policy": "invite_only"},
        ).status_code
        == 403
    )


def test_admin_can_change_membership_policy(monkeypatch) -> None:
    client = _client(UserAppRole.ADMIN, monkeypatch)

    response = client.patch(
        "/users/preference/membership",
        json={"domain_join_policy": "invite_only"},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"domain_join_policy": "invite_only"}
