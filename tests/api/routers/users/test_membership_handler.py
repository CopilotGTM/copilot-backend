"""Organisation-admin member invitation API contract."""

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.api.routers.users import membership_handler as module
from src.domain.models.master_tables import UserAppRole, UserAuthenticationMethod


def _payload():
    return {
        "email": "invitee@acme.com",
        "first_name": "Invitee",
        "last_name": "User",
        "authentication_method": "google_workspace",
    }


def _client(role, monkeypatch):
    organisation_id = uuid4()
    current_user = SimpleNamespace(
        user_id=uuid4(),
        organisation_id=organisation_id,
        user_role=role,
    )
    organisation = SimpleNamespace(organisation_id=organisation_id)

    class _Session:
        async def get(self, _model, _identifier):
            return organisation

    class _Service:
        def __init__(self, _session):
            pass

        async def invite_member(self, **kwargs):
            request = kwargs["request"]
            return SimpleNamespace(
                user_id=uuid4(),
                email=request.email,
                first_name=request.first_name,
                authentication_method=UserAuthenticationMethod.GOOGLE_WORKSPACE,
            )

    async def master_session():
        yield _Session()

    sent = []

    async def send_invitation(**kwargs):
        sent.append(kwargs["to_email"])
        return True

    monkeypatch.setattr(module, "OrganisationMembershipService", _Service)
    monkeypatch.setattr(
        module.email_service, "send_external_auth_invitation_email", send_invitation
    )
    app = FastAPI()
    app.include_router(module.organisation_membership_router, prefix="/users/organisation")
    app.dependency_overrides[module.user_auth] = lambda: current_user
    app.dependency_overrides[module.get_master_async_session] = master_session
    app.dependency_overrides[module.rate_limit_user_write] = lambda: None
    return TestClient(app), sent


def test_member_cannot_invite(monkeypatch) -> None:
    client, sent = _client(UserAppRole.MEMBER, monkeypatch)

    response = client.post("/users/organisation/invitations", json=_payload())

    assert response.status_code == 403
    assert sent == []


def test_admin_can_invite_member(monkeypatch) -> None:
    client, sent = _client(UserAppRole.ADMIN, monkeypatch)

    response = client.post("/users/organisation/invitations", json=_payload())

    assert response.status_code == 200
    assert response.json()["data"]["email"] == "invitee@acme.com"
    assert sent == ["invitee@acme.com"]
