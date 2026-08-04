from types import SimpleNamespace

import pytest
from src.domain.models.master_tables import (
    AdminUserStatus,
    OrganisationStatus,
    SupabaseUserStatus,
    UserAuthenticationMethod,
)
from src.service.users.auth_access_service import AuthAccessError, AuthAccessService


def _user(method: UserAuthenticationMethod, *, active: bool = True):
    return SimpleNamespace(
        authentication_method=method,
        status=SupabaseUserStatus.ACTIVE if active else SupabaseUserStatus.INACTIVE,
        admin_status=AdminUserStatus.APPROVED if active else AdminUserStatus.SUSPENDED,
    )


def _organisation(*, active: bool = True):
    return SimpleNamespace(
        is_active=active,
        status=OrganisationStatus.APPROVED if active else OrganisationStatus.SUSPENDED,
    )


@pytest.mark.parametrize("method", list(UserAuthenticationMethod))
def test_matching_active_authentication_method_is_allowed(method) -> None:
    AuthAccessService.ensure_access_allowed(_user(method), _organisation(), method)


@pytest.mark.parametrize(
    ("configured", "session", "code"),
    [
        (
            UserAuthenticationMethod.PASSWORD,
            UserAuthenticationMethod.GOOGLE_WORKSPACE,
            "PASSWORD_SIGN_IN_REQUIRED",
        ),
        (
            UserAuthenticationMethod.GOOGLE_WORKSPACE,
            UserAuthenticationMethod.PASSWORD,
            "GOOGLE_SIGN_IN_REQUIRED",
        ),
        (
            UserAuthenticationMethod.MICROSOFT_ENTRA,
            UserAuthenticationMethod.PASSWORD,
            "MICROSOFT_SIGN_IN_REQUIRED",
        ),
    ],
)
def test_wrong_authentication_method_is_rejected(configured, session, code) -> None:
    with pytest.raises(AuthAccessError, match=code):
        AuthAccessService.ensure_access_allowed(_user(configured), _organisation(), session)


def test_inactive_user_is_rejected_before_method_disclosure() -> None:
    with pytest.raises(AuthAccessError, match="ACCOUNT_INACTIVE"):
        AuthAccessService.ensure_access_allowed(
            _user(UserAuthenticationMethod.GOOGLE_WORKSPACE, active=False),
            _organisation(),
            UserAuthenticationMethod.PASSWORD,
        )


def test_inactive_organisation_is_rejected() -> None:
    with pytest.raises(AuthAccessError, match="ORGANISATION_INACTIVE"):
        AuthAccessService.ensure_access_allowed(
            _user(UserAuthenticationMethod.PASSWORD),
            _organisation(active=False),
            UserAuthenticationMethod.PASSWORD,
        )
