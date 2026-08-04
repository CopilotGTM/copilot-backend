"""Regression coverage for provider-verified organisation signup routes.

The tests keep public OAuth identity authoritative across preflight, submission,
and status reads without requiring a real Supabase project or database.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.api.routers.users import signup_handler as module
from src.domain.models.master_tables import SignupRequestStatus, UserAuthenticationMethod
from src.domain.models.user_signup_models import SignupFlow
from src.infra.auth.supabase_user_client import SupabaseAuthError, VerifiedSupabaseSession


def _request() -> dict[str, object]:
    return {"organisation_name": "Acme"}


def _verified_session(provider: str = "google") -> VerifiedSupabaseSession:
    method = (
        UserAuthenticationMethod.GOOGLE_WORKSPACE
        if provider == "google"
        else UserAuthenticationMethod.MICROSOFT_ENTRA
    )
    identity_data = {
        "sub": "provider-user",
        "email": "founder@acme.com",
        "email_verified": True,
        **(
            {"custom_claims": {"hd": "acme.com"}}
            if provider == "google"
            else {"custom_claims": {"tid": "00000000-0000-4000-8000-000000000001"}}
        ),
    }
    return VerifiedSupabaseSession(
        user={
            "id": "supabase-user",
            "email": "founder@acme.com",
            "email_confirmed_at": "2026-08-02T00:00:00Z",
            "user_metadata": {
                "full_name": "Founding Admin",
                "avatar_url": "https://lh3.googleusercontent.com/avatar.png",
            },
            "identities": [
                {
                    "id": "provider-user",
                    "provider": "google" if provider == "google" else "azure",
                    "identity_data": identity_data,
                }
            ],
        },
        claims={"sub": "supabase-user"},
        supabase_user_id="supabase-user",
        authentication_method=method,
    )


def _client(
    monkeypatch,
    provider: str = "google",
    *,
    initial_status: SignupRequestStatus = SignupRequestStatus.DRAFT,
    flow: SignupFlow = SignupFlow.CREATE_ORGANISATION,
):
    signup_request_id = uuid4()
    signup = SimpleNamespace(
        signup_request_id=signup_request_id,
        status=initial_status,
        organisation_id=None,
        user_id=None,
        organisation_name=None,
    )
    captured_identities = []

    class _SignupService:
        def __init__(self, _session):
            pass

        async def preflight(self, **_kwargs):
            """Return the server-owned next action exposed by signup preflight."""
            captured_identities.append(_kwargs["identity"])
            return SimpleNamespace(flow=flow, signup=signup)

        async def finalize_signup(self, **_kwargs):
            """Capture the verified identity without writing signup state."""
            captured_identities.append(_kwargs["identity"])
            if signup.status == SignupRequestStatus.DRAFT:
                signup.status = SignupRequestStatus.RECEIVED
            return signup

        async def get_owned_request(self, _request_id, _identity):
            """Return the stable request used to exercise owner-scoped status reads."""
            return signup

    class _MembershipService:
        def __init__(self, _session):
            pass

        async def join_verified_identity(self, _identity, _request_id):
            signup.status = SignupRequestStatus.ACTIVE
            signup.organisation_id = uuid4()
            signup.user_id = uuid4()
            return SimpleNamespace(
                signup=signup,
                status=signup.status,
                user=SimpleNamespace(email="founder@acme.com"),
                notify_admins=True,
            )

    @asynccontextmanager
    async def session_scope():
        """Replace the master database boundary with a no-op async session."""
        yield object()

    monkeypatch.setattr(module, "SignupService", _SignupService)
    monkeypatch.setattr(module, "OrganisationMembershipService", _MembershipService, raising=False)
    monkeypatch.setattr(module, "master_async_session_scope", session_scope)
    monkeypatch.setattr(
        module.supabase_user_client,
        "verify_session",
        lambda _token: _verified_session(provider),
    )
    dispatched = []

    async def dispatch(request_id):
        """Capture worker dispatch so rejected requests prove they have no side effect."""
        dispatched.append(request_id)
        return "task-id"

    monkeypatch.setattr(module, "send_signup_provisioning_task", dispatch)
    membership_notifications = []

    async def notify_membership_admins(result, identity):
        membership_notifications.append((result.status, identity.claims.email))

    monkeypatch.setattr(
        module,
        "_notify_membership_admins",
        notify_membership_admins,
        raising=False,
    )
    app = FastAPI()
    app.include_router(module.router, prefix="/users/auth")
    app.dependency_overrides[module.rate_limit_signup] = lambda: None
    app.dependency_overrides[module.rate_limit_signup_status] = lambda: None
    client = TestClient(app)
    client.membership_notifications = membership_notifications
    return client, signup_request_id, dispatched, captured_identities


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_provider_signup_returns_accepted_and_dispatches_uuid(monkeypatch, provider) -> None:
    """A verified founder starts one durable request using the provider-owned domain."""
    client, signup_request_id, dispatched, captured_identities = _client(monkeypatch, provider)

    response = client.post(
        f"/users/auth/{provider}/signup",
        headers={"Authorization": "Bearer a.b.c"},
        json=_request(),
    )

    assert response.status_code == 202
    assert response.json()["data"]["signup_request_id"] == str(signup_request_id)
    assert dispatched == [signup_request_id]
    assert captured_identities[0].primary_domain == "acme.com"


def test_received_signup_retry_redispatches_after_an_uncertain_queue_result(monkeypatch) -> None:
    """A retry repairs a commit-before-dispatch failure instead of stranding the signup."""
    client, signup_request_id, dispatched, _ = _client(
        monkeypatch,
        initial_status=SignupRequestStatus.RECEIVED,
    )

    response = client.post(
        "/users/auth/google/signup",
        headers={"Authorization": "Bearer a.b.c"},
        json=_request(),
    )

    assert response.status_code == 202
    assert dispatched == [signup_request_id]


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_signup_preflight_creates_resumable_draft(monkeypatch, provider) -> None:
    """Preflight returns provider identity together with its durable draft."""
    client, signup_request_id, dispatched, captured_identities = _client(monkeypatch, provider)

    response = client.put(
        f"/users/auth/{provider}/signup/draft",
        headers={"Authorization": "Bearer a.b.c"},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "flow": "create_organisation",
        "signup_request_id": str(signup_request_id),
        "status": "draft",
        "provider": provider,
        "email": "founder@acme.com",
        "primary_domain": "acme.com",
        "full_name": "Founding Admin",
        "profile_image_url": (
            "https://lh3.googleusercontent.com/avatar.png" if provider == "google" else None
        ),
        "organisation_name": None,
    }
    assert dispatched == []
    assert captured_identities[0].primary_domain == "acme.com"


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_existing_open_domain_returns_join_flow(monkeypatch, provider) -> None:
    client, signup_request_id, dispatched, _ = _client(
        monkeypatch,
        provider,
        flow=SignupFlow.JOIN_EXISTING_ORGANISATION,
    )

    response = client.put(
        f"/users/auth/{provider}/signup/draft",
        headers={"Authorization": "Bearer a.b.c"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["flow"] == "join_existing_organisation"
    assert response.json()["data"]["signup_request_id"] == str(signup_request_id)
    assert response.json()["data"]["organisation_name"] is None
    assert dispatched == []


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_existing_domain_join_activates_member_and_notifies_admins(monkeypatch, provider) -> None:
    client, signup_request_id, dispatched, _ = _client(monkeypatch, provider)

    response = client.post(
        f"/users/auth/{provider}/signup/join",
        headers={"Authorization": "Bearer a.b.c"},
        json={"signup_request_id": str(signup_request_id)},
    )

    assert response.status_code == 202
    assert response.json()["data"]["status"] == "active"
    assert client.membership_notifications == [(SignupRequestStatus.ACTIVE, "founder@acme.com")]
    assert dispatched == []


def test_join_success_is_not_reversed_by_notification_failure(monkeypatch) -> None:
    client, signup_request_id, _, _ = _client(monkeypatch)

    async def fail_notification(_result, _identity):
        raise RuntimeError("notification database unavailable")

    monkeypatch.setattr(module, "_notify_membership_admins", fail_notification)

    response = client.post(
        "/users/auth/google/signup/join",
        headers={"Authorization": "Bearer a.b.c"},
        json={"signup_request_id": str(signup_request_id)},
    )

    assert response.status_code == 202
    assert response.json()["data"]["status"] == "active"


def test_invitation_required_response_does_not_disclose_organisation() -> None:
    signup = SimpleNamespace(
        signup_request_id=uuid4(),
        status=SignupRequestStatus.INVITATION_REQUIRED,
        organisation_id=uuid4(),
        user_id=None,
    )

    response = module._response(signup)

    assert response.organisation_id is None
    assert response.user_id is None


def test_signup_preflight_rejects_google_identity_without_hosted_domain(monkeypatch) -> None:
    """A Google email alone cannot expose the organisation form without Workspace proof."""
    client, _, dispatched, _ = _client(monkeypatch)
    verified = _verified_session()
    verified.user["identities"][0]["identity_data"]["custom_claims"].pop("hd")
    monkeypatch.setattr(module.supabase_user_client, "verify_session", lambda _token: verified)

    response = client.put(
        "/users/auth/google/signup/draft",
        headers={"Authorization": "Bearer a.b.c"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "CONSUMER_ACCOUNT_NOT_SUPPORTED"
    assert dispatched == []


def test_signup_requires_bearer_but_not_browser_idempotency_key(monkeypatch) -> None:
    """The provider session owns request identity; the browser sends no idempotency key."""
    client, _, _, _ = _client(monkeypatch)

    missing_bearer = client.post(
        "/users/auth/google/signup",
        json=_request(),
    )
    valid_request = client.post(
        "/users/auth/google/signup",
        headers={"Authorization": "Bearer a.b.c"},
        json=_request(),
    )

    assert missing_bearer.status_code == 401
    assert valid_request.status_code == 202


def test_signup_returns_unauthorized_for_invalid_verified_session(monkeypatch) -> None:
    """An invalid Supabase session cannot create state or dispatch provisioning."""
    client, _, dispatched, _ = _client(monkeypatch)

    def reject_session(_token):
        """Simulate remote rejection without exposing provider-specific details."""
        raise SupabaseAuthError("Invalid session", status_code=401)

    monkeypatch.setattr(module.supabase_user_client, "verify_session", reject_session)

    response = client.post(
        "/users/auth/google/signup",
        headers={"Authorization": "Bearer invalid.token.value"},
        json=_request(),
    )

    assert response.status_code == 401
    assert response.json()["code"] == "AUTHENTICATION_FAILED"
    assert dispatched == []


@pytest.mark.parametrize(
    "field",
    ["first_name", "country", "terms_version", "primary_domain", "website_url"],
)
def test_signup_rejects_every_field_except_organisation_name(monkeypatch, field) -> None:
    """Extra browser fields cannot override provider or server-owned signup data."""
    client, _, dispatched, captured_identities = _client(monkeypatch)
    request = _request()
    request[field] = "attacker-controlled"

    response = client.post(
        "/users/auth/google/signup",
        headers={"Authorization": "Bearer a.b.c"},
        json=request,
    )

    assert response.status_code == 422
    assert dispatched == []
    assert captured_identities == []


def test_signup_status_requires_provider_session_ownership(monkeypatch) -> None:
    """Status reads succeed only through the same verified provider identity boundary."""
    client, signup_request_id, _, _ = _client(monkeypatch)

    response = client.get(
        f"/users/auth/signup-requests/{signup_request_id}",
        headers={"Authorization": "Bearer a.b.c"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "draft"
