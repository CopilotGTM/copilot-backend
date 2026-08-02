import re
from dataclasses import dataclass
from typing import Any, TypedDict, cast

import jwt
from httpx import Client as HttpxClient
from src.domain.models.master_tables import UserAuthenticationMethod
from src.infra.config.configs import supabase_config
from src.infra.config.constant import SupabaseErrors
from src.infra.utils.logger import get_logger
from supabase import Client, create_client
from supabase.lib.client_options import SyncClientOptions

logger = get_logger()


@dataclass(frozen=True)
class VerifiedSupabaseSession:
    """Remote-verified Supabase user plus method claims from the same token."""

    user: dict[str, Any]
    claims: dict[str, Any]
    supabase_user_id: str
    authentication_method: UserAuthenticationMethod


class AdminUserAttributes(TypedDict, total=False):
    email: str
    password: str
    email_confirm: bool
    user_metadata: dict[str, Any]


class SupabaseAuthError(Exception):
    """
    Custom exception for Supabase authentication errors.

    Attributes:
        message: Error message
        status_code: HTTP status code
        details: Additional error details
    """

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        details: dict[str, Any] | None = None,
    ):
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)


class SupabaseUserClient:
    """
    Supabase client for user-facing authentication.

    Credentials are resolved at startup via ``initialize()`` which loads
    from Key Vault (or env vars for local dev). Call ``initialize()`` in
    the FastAPI lifespan before any request is served.
    """

    def __init__(self):
        self.supabase_url: str = ""
        self.supabase_anon_key: str = ""
        self.supabase_service_role_key: str = ""
        self.client: Client | None = None
        self.admin_client: Client | None = None
        self._auth_httpx_client: HttpxClient | None = None

    async def initialize(self) -> None:
        """Load credentials from env or Key Vault and create Supabase clients.

        Called once during app startup (FastAPI lifespan).
        """
        url, anon_key, service_role_key, _ = await supabase_config.get_credentials()
        self.supabase_url = url
        self.supabase_anon_key = anon_key
        self.supabase_service_role_key = service_role_key

        self._validate_config()

        self._auth_httpx_client = HttpxClient(verify=True, follow_redirects=True, http2=True)
        self.client = create_client(
            self.supabase_url,
            self.supabase_anon_key,
            options=SyncClientOptions(httpx_client=self._auth_httpx_client),
        )
        self.admin_client = create_client(self.supabase_url, self.supabase_service_role_key)
        logger.debug("Supabase User client initialized")

    def _validate_config(self) -> None:
        """
        Validate that all required Supabase configuration is present.
        """
        missing_configs = []

        if not self.supabase_url:
            missing_configs.append("SUPABASE_URL")
        if not self.supabase_anon_key:
            missing_configs.append("SUPABASE_ANON_KEY")
        if not self.supabase_service_role_key:
            missing_configs.append("SUPABASE_SERVICE_ROLE_KEY")

        if missing_configs:
            raise SupabaseAuthError(
                SupabaseErrors.MISSING_CONFIG.format(", ".join(missing_configs)),
                status_code=500,
            )

    def _validate_user_client_config(self) -> None:
        """Validate only the configuration required for user-auth client creation."""
        missing_configs = []

        if not self.supabase_url:
            missing_configs.append("SUPABASE_URL")
        if not self.supabase_anon_key:
            missing_configs.append("SUPABASE_ANON_KEY")

        if missing_configs:
            raise SupabaseAuthError(
                SupabaseErrors.MISSING_CONFIG.format(", ".join(missing_configs)),
                status_code=500,
            )

    def _require_user_base_client(self) -> Client:
        """Return the initialized user client or raise a configuration error."""
        if self.client is None:
            raise SupabaseAuthError("Supabase user client is not initialized", status_code=500)
        return self.client

    def _require_admin_client(self) -> Client:
        """Return the initialized admin client or raise a configuration error."""
        if self.admin_client is None:
            raise SupabaseAuthError("Supabase admin client is not initialized", status_code=500)
        return self.admin_client

    def _new_user_client(self) -> Client:
        """Return a fresh auth client backed by the startup-time HTTP/2 transport."""
        self._validate_user_client_config()
        if self._auth_httpx_client is None:
            raise SupabaseAuthError(
                "Supabase user client is not initialized",
                status_code=500,
            )

        base_client = self._require_user_base_client()
        options = SyncClientOptions(
            schema=base_client.options.schema,
            headers=base_client.options.headers.copy(),
            auto_refresh_token=base_client.options.auto_refresh_token,
            persist_session=base_client.options.persist_session,
            storage=SyncClientOptions().storage,
            realtime=base_client.options.realtime,
            httpx_client=self._auth_httpx_client,
            postgrest_client_timeout=base_client.options.postgrest_client_timeout,
            storage_client_timeout=base_client.options.storage_client_timeout,
            function_client_timeout=base_client.options.function_client_timeout,
            flow_type=base_client.options.flow_type,
        )
        return create_client(self.supabase_url, self.supabase_anon_key, options=options)

    def _validate_email(self, email: str) -> bool:
        """
        Validate email format using regex pattern.

        Args:
            email: Email address to validate

        Returns:
            True if email format is valid, False otherwise
        """
        if not email or not isinstance(email, str):
            return False
        email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        return bool(re.match(email_pattern, email))

    def _validate_password(self, password: str) -> bool:
        """
        Validate password strength requirements.

        Args:
            password: Password to validate

        Returns:
            True if password meets requirements, False otherwise
        """
        if not password or not isinstance(password, str):
            return False
        # Minimum 6 characters
        return len(password) >= 6

    def _validate_user_id(self, user_id: str) -> bool:
        """
        Validate user ID format (UUID).

        Args:
            user_id: User ID to validate

        Returns:
            True if user ID format is valid, False otherwise
        """
        if not user_id or not isinstance(user_id, str):
            return False
        # UUID pattern
        uuid_pattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
        return bool(re.match(uuid_pattern, user_id.lower()))

    def _validate_token(self, token: str, is_refresh: bool = False) -> bool:
        """
        Validate token format.

        Args:
            token: Token to validate
            is_refresh: If True, treat as refresh token (opaque format)

        Returns:
            True if token format is valid, False otherwise
        """
        if not token or not isinstance(token, str):
            return False
        if is_refresh:
            # Refresh tokens are often opaque, long strings—not JWTs
            return len(token.strip()) > 0
        # JWT token validation (3 parts separated by dots)
        parts = token.split(".")
        return len(parts) == 3

    def _validate_hashed_token(self, hashed_token: str) -> bool:
        """
        Validate hashed token format (for password reset).

        Args:
            hashed_token: Hashed token to validate

        Returns:
            True if hashed token format is valid, False otherwise
        """
        if not hashed_token or not isinstance(hashed_token, str):
            return False
        # Hashed tokens are typically base64 encoded strings
        # They don't have the JWT structure (3 parts with dots)
        # Just check if it's a non-empty string
        return len(hashed_token.strip()) > 0

    def sign_in_with_password(self, email: str, password: str) -> dict[str, Any]:
        """
        Sign in user with email and password.

        Args:
            email: User's email
            password: User's password

        Returns:
            Supabase response with user data and tokens
        """
        try:
            # Input validation
            if not self._validate_email(email):
                raise SupabaseAuthError(SupabaseErrors.INVALID_EMAIL, status_code=400)
            if not self._validate_password(password):
                raise SupabaseAuthError(SupabaseErrors.INVALID_PASSWORD, status_code=400)

            response = self._new_user_client().auth.sign_in_with_password(
                {"email": email, "password": password}
            )
            logger.debug("User signed in successfully")
            return response.model_dump()
        except Exception as e:
            logger.warning("Sign in failed for %s: %s", email, str(e))
            raise SupabaseAuthError(
                SupabaseErrors.SIGN_IN_FAILED.format(str(e)), status_code=401
            ) from e

    def generate_password_reset_link(self, email: str) -> str:
        """
        Generate a password reset link and extract the token parameter.

        Args:
            email: User's email address

        Returns:
            The token parameter value from the reset link

        Raises:
            SupabaseAuthError: If link generation fails
        """
        try:
            # Input validation
            if not self._validate_email(email):
                raise SupabaseAuthError(SupabaseErrors.INVALID_EMAIL, status_code=400)

            # Get APP_URL from environment
            import os

            app_url = os.getenv("APP_URL", "http://localhost:3000")
            redirect_url = f"{app_url}/reset-password"

            # Generate recovery link using admin client (doesn't send email)
            response = self._require_admin_client().auth.admin.generate_link(
                {"type": "recovery", "email": email}
            )

            # The response should contain the recovery link
            response_data = response.model_dump()

            hashed_token = response_data.get("properties", {}).get("hashed_token")

            if not hashed_token:
                raise SupabaseAuthError(
                    SupabaseErrors.HASH_TOKEN_GENERATION_FAILED, status_code=500
                )

            logger.info(f"Password reset link generated for user: {email}")
            return f"{redirect_url}?token_hash={hashed_token}"

        except Exception as e:
            logger.error(f"Failed to generate password reset link for {email}: {str(e)}")
            raise SupabaseAuthError(
                SupabaseErrors.RESET_LINK_FAILED.format(str(e)), status_code=500
            ) from e

    def create_user(
        self, email: str, password: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Create a new user using admin client.

        Args:
            email: User's email
            password: User's password
            metadata: Optional user metadata

        Returns:
            Created user data
        """
        try:
            # Input validation
            if not self._validate_email(email):
                raise SupabaseAuthError(SupabaseErrors.INVALID_EMAIL, status_code=400)
            if not self._validate_password(password):
                raise SupabaseAuthError(SupabaseErrors.INVALID_PASSWORD, status_code=400)

            user_data: AdminUserAttributes = {
                "email": email,
                "password": password,
                "email_confirm": True,
            }
            if metadata:
                user_data["user_metadata"] = metadata

            response = self._require_admin_client().auth.admin.create_user(cast(Any, user_data))
            logger.info(f"User created: {email}")
            return response.model_dump()
        except Exception as e:
            logger.error(f"User creation failed for {email}: {str(e)}")
            raise SupabaseAuthError(
                SupabaseErrors.USER_CREATION_FAILED.format(str(e)), status_code=500
            ) from e

    def delete_user(self, user_id: str) -> None:
        """Delete a just-created Supabase user during provisioning compensation."""
        try:
            if not self._validate_user_id(user_id):
                raise SupabaseAuthError(SupabaseErrors.INVALID_USER_ID, status_code=400)
            self._require_admin_client().auth.admin.delete_user(user_id)
        except Exception as exc:
            raise SupabaseAuthError(
                f"Failed to compensate Supabase user creation: {exc}",
                status_code=500,
            ) from exc

    def revoke_session(self, access_token: str, *, global_signout: bool = False) -> None:
        """Revoke refresh tokens represented by an authenticated access token."""
        try:
            if not self._validate_token(access_token):
                raise SupabaseAuthError(SupabaseErrors.INVALID_TOKEN, status_code=400)
            self._require_admin_client().auth.admin.sign_out(
                access_token,
                scope="global" if global_signout else "local",
            )
        except Exception as exc:
            raise SupabaseAuthError(
                SupabaseErrors.SIGN_OUT_FAILED.format(str(exc)),
                status_code=500,
            ) from exc

    def set_user_banned(self, user_id: str, *, banned: bool) -> None:
        """Block or restore Supabase sign-in and refresh for an app user."""
        try:
            if not self._validate_user_id(user_id):
                raise SupabaseAuthError(SupabaseErrors.INVALID_USER_ID, status_code=400)
            self._require_admin_client().auth.admin.update_user_by_id(
                user_id,
                {"ban_duration": "876000h" if banned else "none"},
            )
        except Exception as exc:
            raise SupabaseAuthError(
                SupabaseErrors.SIGN_OUT_FAILED.format(str(exc)),
                status_code=500,
            ) from exc

    def verify_token(self, token: str) -> dict[str, Any]:
        """
        Verify a JWT token and return user information.

        Args:
            token: JWT token to verify

        Returns:
            User information from token
        """
        try:
            # Input validation
            if not self._validate_token(token):
                raise SupabaseAuthError(SupabaseErrors.INVALID_TOKEN, status_code=400)

            response = self._new_user_client().auth.get_user(token)
            if response is None:
                raise SupabaseAuthError(
                    SupabaseErrors.TOKEN_VERIFICATION_FAILED.format("No user returned"),
                    status_code=401,
                )
            logger.debug("Token verified successfully")
            return response.model_dump()
        except Exception as e:
            logger.warning(
                "Token verification failed",
                extra={
                    "event": "auth_failure",
                    "auth_operation": "verify_token",
                    "failure_reason": str(e),
                },
            )
            raise SupabaseAuthError(
                SupabaseErrors.TOKEN_VERIFICATION_FAILED.format(str(e)), status_code=401
            ) from e

    def verify_session(self, token: str) -> VerifiedSupabaseSession:
        """Verify remotely, then read the method claims from that same JWT."""
        verified = self.verify_token(token)
        user = cast(dict[str, Any], verified.get("user") or {})
        supabase_user_id = str(user.get("id") or "")
        try:
            claims = cast(
                dict[str, Any],
                jwt.decode(
                    token,
                    options={
                        "verify_signature": False,
                        "verify_exp": False,
                        "verify_aud": False,
                    },
                ),
            )
            if not supabase_user_id or claims.get("sub") != supabase_user_id:
                raise ValueError("Verified user and token subject differ")
            method = self._authentication_method_from_claims(user, claims)
        except (ValueError, TypeError, jwt.PyJWTError) as exc:
            raise SupabaseAuthError("Unsupported authentication session", status_code=401) from exc

        return VerifiedSupabaseSession(
            user=user,
            claims=claims,
            supabase_user_id=supabase_user_id,
            authentication_method=method,
        )

    @staticmethod
    def _authentication_method_from_claims(
        user: dict[str, Any], claims: dict[str, Any]
    ) -> UserAuthenticationMethod:
        custom_method = claims.get("authentication_method")
        if custom_method:
            return UserAuthenticationMethod(str(custom_method))

        amr = claims.get("amr") or []
        methods = {
            str(entry.get("method")) if isinstance(entry, dict) else str(entry) for entry in amr
        }
        if "oauth" in methods:
            app_metadata = user.get("app_metadata") or {}
            provider = str(app_metadata.get("provider") or claims.get("provider") or "").lower()
            providers = {
                str(value).lower() for value in app_metadata.get("providers") or [provider]
            }
            if len(providers) != 1:
                raise ValueError(
                    "Linked OAuth identities require a signed authentication_method claim"
                )
            if provider == "google":
                return UserAuthenticationMethod.GOOGLE_WORKSPACE
            if provider in {"azure", "azure_ad", "microsoft"}:
                return UserAuthenticationMethod.MICROSOFT_ENTRA
        if "password" in methods:
            return UserAuthenticationMethod.PASSWORD
        raise ValueError("Missing supported authentication method")

    def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        """
        Refresh access token using refresh token.

        Args:
            refresh_token: Refresh token

        Returns:
            New session data with access and refresh tokens
        """
        try:
            # Input validation: refresh token is not always JWT
            if not self._validate_token(refresh_token, is_refresh=True):
                raise SupabaseAuthError(SupabaseErrors.INVALID_TOKEN, status_code=400)

            response = self._new_user_client().auth.refresh_session(refresh_token)
            if response is None:
                raise SupabaseAuthError(
                    SupabaseErrors.TOKEN_REFRESH_FAILED.format("No session returned"),
                    status_code=401,
                )
            logger.debug("Access token refreshed successfully")
            return response.model_dump()
        except Exception as e:
            logger.warning(
                "Token refresh failed",
                extra={
                    "event": "auth_failure",
                    "auth_operation": "refresh_access_token",
                    "failure_reason": str(e),
                },
            )
            raise SupabaseAuthError(
                SupabaseErrors.TOKEN_REFRESH_FAILED.format(str(e)), status_code=401
            ) from e

    def reset_password_with_token(self, token_hash: str, new_password: str) -> str:
        """
        Reset password using a recovery token_hash.

        Args:
            token_hash: Recovery token hash from password reset link
            new_password: New password

        Returns:
            Supabase user ID for which the password was reset

        Raises:
            SupabaseAuthError: If password reset fails
        """
        try:
            # Input validation
            if not self._validate_hashed_token(token_hash):
                raise SupabaseAuthError(SupabaseErrors.INVALID_HASHED_TOKEN, status_code=400)
            if not self._validate_password(new_password):
                raise SupabaseAuthError(SupabaseErrors.INVALID_PASSWORD, status_code=400)

            # Use the recovery token_hash to verify and reset password
            response = self._new_user_client().auth.verify_otp(
                {"token_hash": token_hash, "type": "recovery"}
            )

            # If verification is successful, update the password
            if response:
                # Get user from the response
                user_data = cast(dict[str, object], response.model_dump())
                user_info = cast(dict[str, object], user_data.get("user", {}))
                user_id = user_info.get("id")

                if isinstance(user_id, str) and self._validate_user_id(user_id):
                    # Update password using admin client
                    self._require_admin_client().auth.admin.update_user_by_id(
                        user_id, {"password": new_password}
                    )

                    logger.info(f"Password reset successful for user ID: {user_id}")
                    return user_id
                else:
                    raise SupabaseAuthError(
                        SupabaseErrors.TOKEN_VERIFICATION_FAILED_NO_ID, status_code=500
                    )
            else:
                raise SupabaseAuthError(
                    SupabaseErrors.TOKEN_VERIFICATION_FAILED_GENERIC, status_code=400
                )

        except Exception as e:
            logger.error(f"Password reset with token failed: {str(e)}")
            raise SupabaseAuthError(
                SupabaseErrors.PASSWORD_RESET_FAILED.format(str(e)), status_code=500
            ) from e

    def verify_recovery_token(self, token_hash: str) -> str:
        """Verify a recovery token without changing credentials."""
        try:
            if not self._validate_hashed_token(token_hash):
                raise SupabaseAuthError(SupabaseErrors.INVALID_HASHED_TOKEN, status_code=400)
            response = self._new_user_client().auth.verify_otp(
                {"token_hash": token_hash, "type": "recovery"}
            )
            user_id = (response.model_dump().get("user") or {}).get("id") if response else None
            if not isinstance(user_id, str) or not self._validate_user_id(user_id):
                raise SupabaseAuthError(
                    SupabaseErrors.TOKEN_VERIFICATION_FAILED_NO_ID,
                    status_code=400,
                )
            return user_id
        except Exception as exc:
            raise SupabaseAuthError(
                SupabaseErrors.TOKEN_VERIFICATION_FAILED_GENERIC,
                status_code=400,
            ) from exc

    def update_password(self, user_id: str, new_password: str) -> None:
        """Update a verified local password account through the admin client."""
        try:
            if not self._validate_user_id(user_id):
                raise SupabaseAuthError(SupabaseErrors.INVALID_USER_ID, status_code=400)
            if not self._validate_password(new_password):
                raise SupabaseAuthError(SupabaseErrors.INVALID_PASSWORD, status_code=400)
            self._require_admin_client().auth.admin.update_user_by_id(
                user_id,
                {"password": new_password},
            )
        except Exception as exc:
            raise SupabaseAuthError(
                SupabaseErrors.PASSWORD_RESET_FAILED.format(str(exc)),
                status_code=400,
            ) from exc

    def update_user_metadata(self, user_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
        """
        Update user metadata using admin client.

        Args:
            user_id: Supabase user ID
            metadata: User metadata to update

        Returns:
            Updated user data
        """
        try:
            # Input validation
            if not self._validate_user_id(user_id):
                raise SupabaseAuthError(SupabaseErrors.INVALID_USER_ID, status_code=400)

            response = self._require_admin_client().auth.admin.update_user_by_id(
                user_id, {"user_metadata": metadata}
            )
            logger.info(f"Metadata updated for user: {user_id}")
            return response.model_dump()
        except Exception as e:
            logger.error(f"Metadata update failed for user {user_id}: {str(e)}")
            raise SupabaseAuthError(
                SupabaseErrors.METADATA_UPDATE_FAILED.format(str(e)), status_code=500
            ) from e

    def get_user_from_token(self, token: str, session) -> Any | None:
        """
        Get user from token and database session.

        Args:
            token: JWT token
            session: Database session

        Returns:
            User object if found, None otherwise
        """
        try:
            # Verify token with Supabase
            user_data = self.verify_token(token)

            # Extract user ID from token
            supabase_user_id = user_data.get("user", {}).get("id")
            if not supabase_user_id:
                return None

            # Find user in database by supabase_user_id
            from sqlmodel import select
            from src.domain.models.master_tables import User

            statement = select(User).where(User.supabase_user_id == supabase_user_id)
            user = session.exec(statement).first()

            return user

        except Exception as e:
            logger.error(f"Failed to get user from token: {str(e)}")
            return None


# Create global instance
supabase_user_client = SupabaseUserClient()
