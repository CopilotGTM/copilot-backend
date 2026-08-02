"""Application-owned authorization policy for verified user sessions."""

from src.domain.models.master_tables import (
    AdminUserStatus,
    Organisation,
    OrganisationStatus,
    SupabaseUserStatus,
    User,
    UserAuthenticationMethod,
)


class AuthAccessError(PermissionError):
    """A stable public authentication policy rejection."""

    def __init__(self, code: str, message: str, status_code: int = 403) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(f"{code}: {message}")


class AuthAccessService:
    """Enforce local user, organisation, and sign-in-method policy."""

    @staticmethod
    def ensure_access_allowed(
        user: User,
        organisation: Organisation,
        session_method: UserAuthenticationMethod,
    ) -> None:
        if (
            user.status != SupabaseUserStatus.ACTIVE
            or user.admin_status != AdminUserStatus.APPROVED
        ):
            raise AuthAccessError("ACCOUNT_INACTIVE", "User account is not active")

        if not organisation.is_active or organisation.status != OrganisationStatus.APPROVED:
            raise AuthAccessError("ORGANISATION_INACTIVE", "Organisation is not active")

        configured_method = UserAuthenticationMethod(user.authentication_method)
        if configured_method == session_method:
            return

        raise AuthAccessService.sign_in_required_error(configured_method)

    @staticmethod
    def sign_in_required_error(
        configured_method: UserAuthenticationMethod,
    ) -> AuthAccessError:
        code, message = {
            UserAuthenticationMethod.PASSWORD: (
                "PASSWORD_SIGN_IN_REQUIRED",
                "Please sign in with email and password",
            ),
            UserAuthenticationMethod.GOOGLE_WORKSPACE: (
                "GOOGLE_SIGN_IN_REQUIRED",
                "Please continue with Google",
            ),
            UserAuthenticationMethod.MICROSOFT_ENTRA: (
                "MICROSOFT_SIGN_IN_REQUIRED",
                "Please continue with Microsoft",
            ),
        }[configured_method]
        return AuthAccessError(code, message, status_code=401)
