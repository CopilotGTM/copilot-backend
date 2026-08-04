"""Normalize and validate supported external OAuth identity connections."""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from src.domain.models.master_tables import UserAuthenticationMethod
from src.domain.models.master_tables.external_identity_models import (
    ExternalIdentityProvider,
)
from src.domain.models.user_signup_models import normalize_signup_domain

FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "msn.com",
    "yahoo.com",
    "icloud.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
}


class ExternalAuthError(ValueError):
    """A verified external identity does not satisfy application policy."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "AUTHENTICATION_FAILED",
        status_code: int = 401,
    ) -> None:
        self.message = message
        self.code = code
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class ExternalAuthClaims:
    """Provider-neutral identity fields persisted in one connection record."""

    authentication_method: UserAuthenticationMethod
    mapping_provider: ExternalIdentityProvider
    supabase_user_id: str
    provider_user_id: str
    provider_tenant_id: str
    email: str
    full_name: str | None
    profile_image_url: str | None


@dataclass(frozen=True)
class ExternalSignupIdentity:
    """Verified provider claims paired with their canonical company domain."""

    claims: ExternalAuthClaims
    primary_domain: str


class ExternalAuthService:
    """Support Google Workspace and Microsoft Entra through one contract."""

    _PROVIDERS = {
        "google": (
            {"google"},
            UserAuthenticationMethod.GOOGLE_WORKSPACE,
            ExternalIdentityProvider.GOOGLE,
        ),
        "microsoft": (
            {"azure", "azure_ad", "microsoft"},
            UserAuthenticationMethod.MICROSOFT_ENTRA,
            ExternalIdentityProvider.AZURE_AD,
        ),
    }

    def extract_claims(
        self,
        supabase_user: dict[str, Any],
        *,
        requested_provider: str,
    ) -> ExternalAuthClaims:
        """Normalize one remotely verified Supabase identity into the shared auth contract."""
        provider_key = requested_provider.strip().lower()
        config = self._PROVIDERS.get(provider_key)
        if not config:
            raise ExternalAuthError("Unsupported authentication provider", status_code=400)
        identity_names, method, mapping_provider = config
        identity = next(
            (
                value
                for value in supabase_user.get("identities") or []
                if isinstance(value, dict)
                and str(value.get("provider") or "").lower() in identity_names
            ),
            None,
        )
        if not identity:
            raise ExternalAuthError("The requested provider identity was not returned")

        identity_data = identity.get("identity_data") or {}
        user_metadata = supabase_user.get("user_metadata") or {}
        supabase_user_id = self._clean(supabase_user.get("id"))
        email = self._clean(supabase_user.get("email") or identity_data.get("email"))
        provider_user_id = self._clean(identity_data.get("sub") or identity.get("id"))
        provider_tenant_id = self._tenant_id(provider_key, identity_data)
        if not supabase_user_id or not email or not provider_user_id:
            raise ExternalAuthError("Invalid external identity")
        if not (
            supabase_user.get("email_confirmed_at") or identity_data.get("email_verified") is True
        ):
            raise ExternalAuthError("External identity email is not verified")

        return ExternalAuthClaims(
            authentication_method=method,
            mapping_provider=mapping_provider,
            supabase_user_id=supabase_user_id,
            provider_user_id=provider_user_id,
            provider_tenant_id=provider_tenant_id,
            email=email.lower(),
            full_name=self._clean(user_metadata.get("full_name") or identity_data.get("name")),
            profile_image_url=self._trusted_profile_image_url(
                user_metadata.get("avatar_url")
                or user_metadata.get("picture")
                or identity_data.get("picture"),
                provider=provider_key,
            ),
        )

    def validate_provider_policy(
        self,
        claims: ExternalAuthClaims,
        *,
        organisation_domains: list[str] | None,
    ) -> None:
        """Require an existing user identity to match its organisation's allowed domains."""
        domains = self.normalize_domains(organisation_domains)
        email_domain = claims.email.rsplit("@", 1)[-1]
        if email_domain in FREE_EMAIL_DOMAINS or email_domain not in domains:
            raise ExternalAuthError(
                "Use your company email to sign in",
                code="AUTHENTICATION_FAILED",
                status_code=403,
            )
        if (
            claims.authentication_method == UserAuthenticationMethod.GOOGLE_WORKSPACE
            and claims.provider_tenant_id.lower() not in domains
        ):
            raise ExternalAuthError(
                "Use your company Google Workspace account to sign in",
                status_code=403,
            )

    def validate_signup_identity(
        self,
        claims: ExternalAuthClaims,
    ) -> ExternalSignupIdentity:
        """Bind public signup to the company domain proven by provider claims."""
        _, separator, email_domain_value = claims.email.strip().lower().rpartition("@")
        try:
            email_domain = normalize_signup_domain(email_domain_value)
        except ValueError as exc:
            raise ExternalAuthError(
                "The verified identity is invalid",
                code="SIGNUP_IDENTITY_INVALID",
                status_code=403,
            ) from exc
        if not separator or email_domain in FREE_EMAIL_DOMAINS:
            raise ExternalAuthError(
                "Consumer accounts cannot create an organisation",
                code="CONSUMER_ACCOUNT_NOT_SUPPORTED",
                status_code=403,
            )

        if claims.authentication_method == UserAuthenticationMethod.GOOGLE_WORKSPACE:
            if not claims.provider_tenant_id:
                raise ExternalAuthError(
                    "A Google Workspace hosted domain is required",
                    code="CONSUMER_ACCOUNT_NOT_SUPPORTED",
                    status_code=403,
                )
            try:
                identity_domain = normalize_signup_domain(claims.provider_tenant_id)
            except ValueError as exc:
                raise ExternalAuthError(
                    "The Google Workspace identity is invalid",
                    code="SIGNUP_IDENTITY_INVALID",
                    status_code=403,
                ) from exc
            if identity_domain != email_domain:
                raise ExternalAuthError(
                    "The verified email and hosted domain do not match",
                    code="SIGNUP_IDENTITY_INVALID",
                    status_code=403,
                )
        elif claims.authentication_method == UserAuthenticationMethod.MICROSOFT_ENTRA:
            try:
                UUID(claims.provider_tenant_id)
            except ValueError as exc:
                raise ExternalAuthError(
                    "A valid Microsoft organisation tenant is required",
                    code="SIGNUP_IDENTITY_INVALID",
                    status_code=403,
                ) from exc
            identity_domain = email_domain
        else:
            raise ExternalAuthError(
                "The verified identity is not eligible for signup",
                code="SIGNUP_IDENTITY_INVALID",
                status_code=403,
            )

        return ExternalSignupIdentity(claims=claims, primary_domain=identity_domain)

    def email_matches_organisation_domain(
        self,
        *,
        email: str,
        organisation_domains: list[str] | None,
    ) -> bool:
        """Match a non-consumer email against normalized organisation domains."""
        email_domain = email.strip().lower().rsplit("@", 1)[-1]
        return email_domain not in FREE_EMAIL_DOMAINS and email_domain in self.normalize_domains(
            organisation_domains
        )

    @staticmethod
    def normalize_domains(domains: list[str] | None) -> set[str]:
        """Canonicalize configured organisation domains for policy comparisons."""
        return {value.strip().lower() for value in domains or [] if value.strip()}

    @staticmethod
    def _tenant_id(provider: str, identity_data: dict[str, Any]) -> str:
        custom_claims = identity_data.get("custom_claims")
        if provider == "google":
            hosted_domain = ExternalAuthService._clean(
                (custom_claims.get("hd") if isinstance(custom_claims, dict) else None)
                or identity_data.get("hd")
            )
            return hosted_domain or ""

        tenant_id = ExternalAuthService._clean(
            (
                custom_claims.get("tid") or custom_claims.get("tenant_id")
                if isinstance(custom_claims, dict)
                else None
            )
            or identity_data.get("tid")
            or identity_data.get("tenant_id")
        )
        try:
            return str(UUID(tenant_id or ""))
        except ValueError as exc:
            raise ExternalAuthError("A verified Microsoft organisation tenant is required") from exc

    @staticmethod
    def _clean(value: object) -> str | None:
        cleaned = str(value).strip() if value is not None else ""
        return cleaned or None

    @staticmethod
    def _trusted_profile_image_url(value: object, *, provider: str) -> str | None:
        cleaned = ExternalAuthService._clean(value)
        if not cleaned or provider != "google":
            return None
        parsed = urlparse(cleaned)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (
            hostname == "googleusercontent.com" or hostname.endswith(".googleusercontent.com")
        ):
            return None
        return cleaned
