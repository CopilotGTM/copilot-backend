"""Provider-policy tests that keep public signup bound to verified work identities."""

from dataclasses import replace

import pytest
from src.domain.models.master_tables import UserAuthenticationMethod
from src.domain.models.master_tables.external_identity_models import (
    ExternalIdentityProvider,
)
from src.service.users.external_auth_service import (
    ExternalAuthClaims,
    ExternalAuthError,
    ExternalAuthService,
)


def _claims(method: UserAuthenticationMethod, **overrides) -> ExternalAuthClaims:
    claims = ExternalAuthClaims(
        authentication_method=method,
        mapping_provider=(
            ExternalIdentityProvider.GOOGLE
            if method == UserAuthenticationMethod.GOOGLE_WORKSPACE
            else ExternalIdentityProvider.AZURE_AD
        ),
        supabase_user_id="supabase-user",
        provider_user_id="provider-user",
        provider_tenant_id=(
            "acme.com"
            if method == UserAuthenticationMethod.GOOGLE_WORKSPACE
            else "00000000-0000-4000-8000-000000000001"
        ),
        email="founder@acme.com",
        full_name="Founding Admin",
        profile_image_url=None,
    )
    return replace(claims, **overrides)


def test_google_workspace_matching_hosted_domain_is_eligible() -> None:
    """A verified Google email and matching hosted domain establish company authority."""
    identity = ExternalAuthService().validate_signup_identity(
        _claims(UserAuthenticationMethod.GOOGLE_WORKSPACE),
    )

    assert identity.primary_domain == "acme.com"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"provider_tenant_id": ""}, "CONSUMER_ACCOUNT_NOT_SUPPORTED"),
        ({"provider_tenant_id": "other.com"}, "SIGNUP_IDENTITY_INVALID"),
        ({"email": "founder@gmail.com"}, "CONSUMER_ACCOUNT_NOT_SUPPORTED"),
    ],
)
def test_google_consumer_or_mismatched_identity_is_rejected(changes, code) -> None:
    """Google signup rejects consumer accounts and conflicting hosted-domain claims."""
    with pytest.raises(ExternalAuthError) as error:
        ExternalAuthService().validate_signup_identity(
            _claims(UserAuthenticationMethod.GOOGLE_WORKSPACE, **changes),
        )

    assert error.value.code == code


def test_microsoft_organisation_tenant_is_eligible() -> None:
    """A verified non-consumer Microsoft identity with a tenant UUID is eligible."""
    identity = ExternalAuthService().validate_signup_identity(
        _claims(UserAuthenticationMethod.MICROSOFT_ENTRA),
    )

    assert identity.primary_domain == "acme.com"


@pytest.mark.parametrize("tenant_id", ["", "consumers", "not-a-uuid"])
def test_microsoft_without_organisation_tenant_is_rejected(tenant_id) -> None:
    """Microsoft consumer or malformed tenant identities cannot found organisations."""
    with pytest.raises(ExternalAuthError) as error:
        ExternalAuthService().validate_signup_identity(
            _claims(
                UserAuthenticationMethod.MICROSOFT_ENTRA,
                provider_tenant_id=tenant_id,
            ),
        )

    assert error.value.code == "SIGNUP_IDENTITY_INVALID"
