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
from src.infra.auth.supabase_user_client import SupabaseAuthError, VerifiedSupabaseSession


def _request() -> dict[str, object]:
    return {
        "first_name": "Founding",
        "last_name": "Admin",
        "organisation_name": "Acme",
        "country": "US",
        "terms_version": "2026-08-02",
        "privacy_version": "2026-08-02",
    }


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
        ("hd" if provider == "google" else "tid"): (
            "acme.com" if provider == "google" else "00000000-0000-4000-8000-000000000001"
        ),
    }
    return VerifiedSupabaseSession(
        user={
            "id": "supabase-user",
            "email": "founder@acme.com",
            "email_confirmed_at": "2026-08-02T00:00:00Z",
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


def _client(monkeypatch, provider: str = "google"):
    signup_request_id = uuid4()
    signup = SimpleNamespace(
        signup_request_id=signup_request_id,
        status=SignupRequestStatus.RECEIVED,
        organisation_id=None,
        user_id=None,
    )
    captured_identities = []

    class _SignupService:
        def __init__(self, _session):
            pass

        async def start_signup(self, **_kwargs):
            """Capture the verified identity without writing signup state."""
            captured_identities.append(_kwargs["identity"])
            return signup

        async def get_owned_request(self, _request_id, _identity):
            """Return the stable request used to exercise owner-scoped status reads."""
            return signup

    @asynccontextmanager
    async def session_scope():
        """Replace the master database boundary with a no-op async session."""
        yield object()

    monkeypatch.setattr(module, "SignupService", _SignupService)
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
    app = FastAPI()
    app.include_router(module.router, prefix="/users/auth")
    app.dependency_overrides[module.rate_limit_signup] = lambda: None
    app.dependency_overrides[module.rate_limit_signup_status] = lambda: None
    return TestClient(app), signup_request_id, dispatched, captured_identities


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_provider_signup_returns_accepted_and_dispatches_uuid(monkeypatch, provider) -> None:
    """A verified founder starts one durable request using the provider-owned domain."""
    client, signup_request_id, dispatched, captured_identities = _client(monkeypatch, provider)

    response = client.post(
        f"/users/auth/{provider}/signup",
        headers={
            "Authorization": "Bearer a.b.c",
            "Idempotency-Key": "a-stable-idempotency-key",
        },
        json=_request(),
    )

    assert response.status_code == 202
    assert response.json()["data"]["signup_request_id"] == str(signup_request_id)
    assert dispatched == [signup_request_id]
    assert captured_identities[0].primary_domain == "acme.com"


def test_signup_preflight_verifies_identity_before_showing_form(monkeypatch) -> None:
    """Preflight returns authoritative identity data without provisioning anything."""
    client, _, dispatched, _ = _client(monkeypatch)

    response = client.get(
        "/users/auth/google/signup",
        headers={"Authorization": "Bearer a.b.c"},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "provider": "google",
        "email": "founder@acme.com",
        "primary_domain": "acme.com",
        "full_name": None,
    }
    assert dispatched == []


def test_signup_preflight_rejects_google_identity_without_hosted_domain(monkeypatch) -> None:
    """A Google email alone cannot expose the organisation form without Workspace proof."""
    client, _, dispatched, _ = _client(monkeypatch)
    verified = _verified_session()
    verified.user["identities"][0]["identity_data"].pop("hd")
    monkeypatch.setattr(module.supabase_user_client, "verify_session", lambda _token: verified)

    response = client.get(
        "/users/auth/google/signup",
        headers={"Authorization": "Bearer a.b.c"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "CONSUMER_ACCOUNT_NOT_SUPPORTED"
    assert dispatched == []


def test_signup_requires_bearer_and_idempotency_key(monkeypatch) -> None:
    """Submission requires both verified identity and replay-safe request identity."""
    client, _, _, _ = _client(monkeypatch)

    missing_bearer = client.post(
        "/users/auth/google/signup",
        headers={"Idempotency-Key": "a-stable-idempotency-key"},
        json=_request(),
    )
    short_key = client.post(
        "/users/auth/google/signup",
        headers={"Authorization": "Bearer a.b.c", "Idempotency-Key": "short"},
        json=_request(),
    )
    blank_key = client.post(
        "/users/auth/google/signup",
        headers={"Authorization": "Bearer a.b.c", "Idempotency-Key": " " * 16},
        json=_request(),
    )

    assert missing_bearer.status_code == 401
    assert short_key.status_code == 400
    assert blank_key.status_code == 400


def test_signup_returns_unauthorized_for_invalid_verified_session(monkeypatch) -> None:
    """An invalid Supabase session cannot create state or dispatch provisioning."""
    client, _, dispatched, _ = _client(monkeypatch)

    def reject_session(_token):
        """Simulate remote rejection without exposing provider-specific details."""
        raise SupabaseAuthError("Invalid session", status_code=401)

    monkeypatch.setattr(module.supabase_user_client, "verify_session", reject_session)

    response = client.post(
        "/users/auth/google/signup",
        headers={
            "Authorization": "Bearer invalid.token.value",
            "Idempotency-Key": "a-stable-idempotency-key",
        },
        json=_request(),
    )

    assert response.status_code == 401
    assert response.json()["code"] == "AUTHENTICATION_FAILED"
    assert dispatched == []


def test_signup_rejects_website_outside_provider_domain(monkeypatch) -> None:
    """Company website input remains bound to the provider-verified domain."""
    client, _, dispatched, _ = _client(monkeypatch)
    request = _request()
    request["website_url"] = "https://attacker.example"

    response = client.post(
        "/users/auth/google/signup",
        headers={
            "Authorization": "Bearer a.b.c",
            "Idempotency-Key": "a-stable-idempotency-key",
        },
        json=request,
    )

    assert response.status_code == 403
    assert response.json()["code"] == "SIGNUP_DOMAIN_MISMATCH"
    assert dispatched == []


def test_signup_rejects_client_supplied_primary_domain(monkeypatch) -> None:
    client, _, dispatched, captured_identities = _client(monkeypatch)
    request = _request()
    request["primary_domain"] = "attacker.example"

    response = client.post(
        "/users/auth/google/signup",
        headers={
            "Authorization": "Bearer a.b.c",
            "Idempotency-Key": "a-stable-idempotency-key",
        },
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
    assert response.json()["data"]["status"] == "received"
