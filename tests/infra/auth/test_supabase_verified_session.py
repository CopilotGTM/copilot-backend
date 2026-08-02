import jwt
import pytest
from src.domain.models.master_tables import UserAuthenticationMethod
from src.infra.auth.supabase_user_client import SupabaseAuthError, SupabaseUserClient


@pytest.mark.parametrize(
    ("amr", "provider", "expected"),
    [
        ([{"method": "password"}], None, UserAuthenticationMethod.PASSWORD),
        ([{"method": "oauth"}], "google", UserAuthenticationMethod.GOOGLE_WORKSPACE),
        ([{"method": "oauth"}], "azure", UserAuthenticationMethod.MICROSOFT_ENTRA),
    ],
)
def test_verified_session_maps_supported_methods(monkeypatch, amr, provider, expected) -> None:
    client = SupabaseUserClient()
    user = {"id": "user-1", "app_metadata": {"provider": provider}}
    monkeypatch.setattr(client, "verify_token", lambda _token: {"user": user})
    token = jwt.encode({"sub": "user-1", "amr": amr}, "test-secret", algorithm="HS256")

    session = client.verify_session(token)

    assert session.supabase_user_id == "user-1"
    assert session.authentication_method == expected


def test_verified_session_rejects_subject_mismatch(monkeypatch) -> None:
    client = SupabaseUserClient()
    monkeypatch.setattr(client, "verify_token", lambda _token: {"user": {"id": "user-1"}})
    token = jwt.encode(
        {"sub": "different-user", "amr": [{"method": "password"}]},
        "test-secret",
        algorithm="HS256",
    )

    with pytest.raises(SupabaseAuthError):
        client.verify_session(token)


def test_verified_session_rejects_ambiguous_linked_oauth_identity(monkeypatch) -> None:
    client = SupabaseUserClient()
    user = {
        "id": "user-1",
        "app_metadata": {"provider": "google", "providers": ["email", "google"]},
    }
    monkeypatch.setattr(client, "verify_token", lambda _token: {"user": user})
    token = jwt.encode(
        {"sub": "user-1", "amr": [{"method": "oauth"}]},
        "test-secret-that-is-long-enough-for-hs256",
        algorithm="HS256",
    )

    with pytest.raises(SupabaseAuthError, match="Unsupported authentication session"):
        client.verify_session(token)


def test_verified_session_accepts_signed_method_for_linked_identity(monkeypatch) -> None:
    client = SupabaseUserClient()
    user = {
        "id": "user-1",
        "app_metadata": {"provider": "email", "providers": ["email", "google"]},
    }
    monkeypatch.setattr(client, "verify_token", lambda _token: {"user": user})
    token = jwt.encode(
        {
            "sub": "user-1",
            "amr": [{"method": "oauth"}],
            "authentication_method": "google_workspace",
        },
        "test-secret-that-is-long-enough-for-hs256",
        algorithm="HS256",
    )

    assert (
        client.verify_session(token).authentication_method
        == UserAuthenticationMethod.GOOGLE_WORKSPACE
    )
