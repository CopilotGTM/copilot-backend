from uuid import uuid4

import pytest
from src.domain.models.master_tables import UserAuthenticationMethod
from src.domain.models.master_tables.external_identity_models import (
    ExternalIdentityProvider,
)
from src.service.users.external_auth_service import (
    ExternalAuthError,
    ExternalAuthService,
)


def _provider_user(provider: str, *, email: str = "person@acme.com") -> dict:
    identity_data = {
        "sub": f"{provider}-subject",
        "email": email,
        "email_verified": True,
    }
    if provider == "google":
        identity_data["hd"] = "acme.com"
        identity_data["picture"] = "https://lh3.googleusercontent.com/avatar"
    else:
        identity_data["tid"] = str(uuid4())
    return {
        "id": "supabase-user",
        "email": email,
        "email_confirmed_at": "2026-08-01T00:00:00Z",
        "identities": [{"provider": provider, "identity_data": identity_data}],
    }


@pytest.mark.parametrize(
    ("provider", "expected_method", "expected_mapping_provider"),
    [
        (
            "google",
            UserAuthenticationMethod.GOOGLE_WORKSPACE,
            ExternalIdentityProvider.GOOGLE,
        ),
        (
            "microsoft",
            UserAuthenticationMethod.MICROSOFT_ENTRA,
            ExternalIdentityProvider.AZURE_AD,
        ),
    ],
)
def test_provider_claims_form_one_connection_contract(
    provider, expected_method, expected_mapping_provider
) -> None:
    identity_provider = "google" if provider == "google" else "azure"
    service = ExternalAuthService()

    claims = service.extract_claims(
        _provider_user(identity_provider),
        requested_provider=provider,
    )

    assert claims.authentication_method == expected_method
    assert claims.mapping_provider == expected_mapping_provider
    service.validate_provider_policy(claims, organisation_domains=["ACME.COM"])


def test_google_workspace_requires_hosted_domain() -> None:
    user = _provider_user("google")
    del user["identities"][0]["identity_data"]["hd"]

    with pytest.raises(ExternalAuthError, match="Google Workspace"):
        ExternalAuthService().validate_provider_policy(
            ExternalAuthService().extract_claims(user, requested_provider="google"),
            organisation_domains=["acme.com"],
        )


def test_microsoft_requires_tenant_claim() -> None:
    user = _provider_user("azure")
    del user["identities"][0]["identity_data"]["tid"]

    with pytest.raises(ExternalAuthError, match="Microsoft organisation"):
        ExternalAuthService().extract_claims(user, requested_provider="microsoft")


def test_consumer_email_is_rejected_for_external_auth() -> None:
    user = _provider_user("google", email="person@gmail.com")
    user["identities"][0]["identity_data"]["hd"] = "gmail.com"
    claims = ExternalAuthService().extract_claims(user, requested_provider="google")

    with pytest.raises(ExternalAuthError, match="company email"):
        ExternalAuthService().validate_provider_policy(
            claims,
            organisation_domains=["gmail.com"],
        )


def test_untrusted_profile_image_is_not_persisted() -> None:
    user = _provider_user("google")
    user["identities"][0]["identity_data"]["picture"] = "https://example.com/avatar"

    claims = ExternalAuthService().extract_claims(user, requested_provider="google")

    assert claims.profile_image_url is None


def test_google_profile_image_allows_https_subdomain() -> None:
    claims = ExternalAuthService().extract_claims(
        _provider_user("google"), requested_provider="google"
    )

    assert claims.profile_image_url == "https://lh3.googleusercontent.com/avatar"
