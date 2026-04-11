"""Fixtures and fake services for Users API tests."""

import importlib
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import fakeredis
import pytest
from fastapi.testclient import TestClient
from src.domain.models.master_tables import SupabaseUserStatus, UserAppRole

logger = logging.getLogger(__name__)

# =============================================================================
# Fake User Model
# =============================================================================


class FakeUser:
    """Fake User model for testing."""

    def __init__(
        self,
        user_id: UUID | None = None,
        email: str = "test@example.com",
        first_name: str = "Test",
        last_name: str = "User",
        user_role: UserAppRole = UserAppRole.MEMBER,
        status: SupabaseUserStatus = SupabaseUserStatus.ACTIVE,
        supabase_user_id: str | None = None,
        email_verified: bool = True,
        last_login_at: datetime | None = None,
        created_at: datetime | None = None,
        organisation_id: UUID | None = None,
        timezone: str | None = None,
    ):
        self.user_id = user_id or uuid4()
        self.email = email
        self.first_name = first_name
        self.last_name = last_name
        self.user_role = user_role
        self.status = status
        self.supabase_user_id = supabase_user_id or str(uuid4())
        self.email_verified = email_verified
        self.last_login_at = last_login_at or datetime.now(UTC)
        self.created_at = created_at or datetime.now(UTC)
        self.organisation_id = organisation_id or uuid4()
        self.timezone = timezone


# =============================================================================
# Fake Services
# =============================================================================


class FakeSupabaseUserClient:
    """Fake Supabase user client for testing."""

    def __init__(self):
        self._users: dict[str, dict[str, Any]] = {}
        self._tokens: dict[str, str] = {}  # token_hash -> user_id
        self._refresh_tokens: dict[str, str] = {}  # refresh_token -> user_id
        self._raise_on_sign_in = False
        self._raise_on_refresh = False

    def add_user(
        self,
        email: str,
        password: str,
        user_id: str,
        requires_password_change: bool = False,
        email_verified: bool = True,
    ):
        """Add a user for testing."""
        self._users[email] = {
            "user_id": user_id,
            "password": password,
            "requires_password_change": requires_password_change,
            "email_verified": email_verified,
        }

    def add_recovery_token(self, token_hash: str, user_id: str):
        """Add a recovery token."""
        self._tokens[token_hash] = user_id

    def add_refresh_token(self, refresh_token: str, user_id: str):
        """Add a refresh token."""
        self._refresh_tokens[refresh_token] = user_id

    def sign_in_with_password(self, email: str, password: str) -> dict:
        """Simulate Supabase sign in."""
        if self._raise_on_sign_in:
            raise Exception("Sign in failed")

        user_data = self._users.get(email)
        if not user_data or user_data["password"] != password:
            raise Exception("Invalid credentials")

        return {
            "user": {
                "id": user_data["user_id"],
                "email": email,
                "user_metadata": {
                    "requires_password_change": user_data["requires_password_change"],
                    "email_verified": user_data["email_verified"],
                },
            },
            "session": {
                "access_token": f"access_token_{uuid4()}",
                "refresh_token": f"refresh_token_{uuid4()}",
            },
        }

    def reset_password_with_token(self, token_hash: str, new_password: str) -> str:
        """Simulate password reset with token."""
        user_id = self._tokens.get(token_hash)
        if not user_id:
            raise Exception("Invalid recovery token")
        return user_id

    def refresh_access_token(self, refresh_token: str) -> dict:
        """Simulate token refresh."""
        if self._raise_on_refresh:
            from src.infra.auth.supabase_user_client import SupabaseAuthError

            raise SupabaseAuthError(401, "Token expired")

        user_id = self._refresh_tokens.get(refresh_token)
        if not user_id:
            return {"session": {}}  # No access token = invalid

        return {
            "session": {
                "access_token": f"new_access_token_{uuid4()}",
                "refresh_token": f"new_refresh_token_{uuid4()}",
            },
        }

    def generate_password_reset_link(self, email: str) -> str:
        """Generate a password reset link."""
        return f"https://example.com/reset?token={uuid4()}"

    def sign_out_user(self, user_id: str, global_signout: bool = False):
        """Sign out user."""
        pass

    def update_user_metadata(self, user_id: str, metadata: dict):
        """Update user metadata."""
        pass


class FakeEmailService:
    """Fake email service for testing."""

    def __init__(self):
        self._should_fail = False
        self._sent_emails: list[dict] = []

    def send_password_reset_email(self, to_email: str, first_name: str, reset_url: str) -> bool:
        """Simulate sending password reset email."""
        if self._should_fail:
            return False
        self._sent_emails.append(
            {"to_email": to_email, "first_name": first_name, "reset_url": reset_url}
        )
        return True


class FakeMasterSession:
    """Fake async session for master DB queries."""

    def __init__(self):
        self._users: dict[str, FakeUser] = {}  # email -> user
        self._users_by_supabase_id: dict[str, FakeUser] = {}  # supabase_user_id -> user
        self._last_query_email: str | None = None
        self._last_query_supabase_id: str | None = None

    def add_user(self, user: FakeUser):
        """Add a user to the fake DB."""
        self._users[user.email] = user
        if user.supabase_user_id:
            self._users_by_supabase_id[user.supabase_user_id] = user

    async def execute(self, statement):
        """Execute a query."""
        # Extract bind parameters from the statement
        try:
            # Try to compile the statement to get bind parameters
            compiled = statement.compile()
            params = compiled.params

            # Check for email parameter
            for _key, value in params.items():
                if isinstance(value, str) and "@" in value:
                    self._last_query_email = value
                    break
                # Check for supabase_user_id (UUID string format)
                if isinstance(value, str) and len(value) == 36 and "-" in value:
                    self._last_query_supabase_id = value
        except Exception as err:
            logger.debug("Failed to parse bind params for fake session: %s", err)

        return FakeResult(
            self._users,
            self._users_by_supabase_id,
            self._last_query_email,
            self._last_query_supabase_id,
        )

    def add(self, obj):
        """Add object to session."""
        pass

    async def commit(self):
        """Commit changes."""
        pass

    async def refresh(self, obj):
        """Refresh object."""
        pass


class FakeResult:
    """Fake query result."""

    def __init__(self, users_by_email, users_by_supabase_id, query_email, query_supabase_id):
        self._users_by_email = users_by_email
        self._users_by_supabase_id = users_by_supabase_id
        self._query_email = query_email
        self._query_supabase_id = query_supabase_id

    def scalars(self):
        return self

    def first(self):
        """Get the first result."""
        # Check by email first
        if self._query_email and self._query_email in self._users_by_email:
            return self._users_by_email[self._query_email]

        # Then check by supabase_user_id
        if self._query_supabase_id and self._query_supabase_id in self._users_by_supabase_id:
            return self._users_by_supabase_id[self._query_supabase_id]

        return None


# =============================================================================
# Factory Functions
# =============================================================================


def create_user(
    email: str = "test@example.com",
    password: str = "password123",
    first_name: str = "Test",
    last_name: str = "User",
    user_role: UserAppRole = UserAppRole.MEMBER,
    requires_password_change: bool = False,
    supabase_user_id: str | None = None,
    timezone: str | None = None,
) -> tuple[FakeUser, str]:
    """Create a test user with matching Supabase data.

    Returns a tuple of (FakeUser, supabase_user_id).
    """
    supabase_id = supabase_user_id or str(uuid4())
    user = FakeUser(
        email=email,
        first_name=first_name,
        last_name=last_name,
        user_role=user_role,
        supabase_user_id=supabase_id,
        timezone=timezone,
    )
    return user, supabase_id


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(scope="function")
def user_auth_test_data():
    """Create test data for user auth tests."""
    supabase_client = FakeSupabaseUserClient()
    email_service = FakeEmailService()
    master_session = FakeMasterSession()

    # Create default test user
    user, supabase_id = create_user(
        email="testuser@example.com",
        password="correctpassword",
    )
    master_session.add_user(user)
    supabase_client.add_user(
        email="testuser@example.com",
        password="correctpassword",
        user_id=supabase_id,
    )

    # Add a valid refresh token
    supabase_client.add_refresh_token("valid_refresh_token", supabase_id)

    # Add a valid recovery token
    supabase_client.add_recovery_token("valid_recovery_token", supabase_id)

    return {
        "supabase_client": supabase_client,
        "email_service": email_service,
        "master_session": master_session,
        "user": user,
    }


@pytest.fixture(scope="function")
def client_with_user_auth(mocker, user_auth_test_data):
    """Create a TestClient with user auth dependencies overridden."""
    import src.api.fastapi_app as fastapi_app
    import src.infra.utils.app_state as app_state

    importlib.reload(app_state)
    importlib.reload(fastapi_app)

    supabase_client = user_auth_test_data["supabase_client"]
    email_service = user_auth_test_data["email_service"]
    master_session = user_auth_test_data["master_session"]
    user = user_auth_test_data["user"]

    # Patch Redis client
    mocker.patch(
        "src.infra.messaging.redis_client.get_redis_client",
        return_value=fakeredis.FakeRedis(),
    )

    # Patch Supabase client
    mocker.patch(
        "src.api.routers.users.auth_handler.supabase_user_client",
        supabase_client,
    )

    # Patch email service
    mocker.patch(
        "src.api.routers.users.auth_handler.email_service",
        email_service,
    )

    @asynccontextmanager
    async def fake_master_async_session_scope():
        yield master_session

    mocker.patch(
        "src.api.routers.users.auth_handler.master_async_session_scope",
        fake_master_async_session_scope,
    )

    # Build ASGI app
    asgi_app = fastapi_app.create_fastapi_app()

    from src.api.dependencies.master_dependencies import get_master_async_session
    from src.api.dependencies.rate_limit import (
        rate_limit_login,
        rate_limit_password_reset,
    )
    from src.infra.auth.user_auth import user_auth
    from src.infra.utils.app_state import fastapp as _fastapp

    # Override dependencies
    async def override_master_session():
        yield master_session

    async def override_rate_limit_login():
        return None

    async def override_rate_limit_password_reset():
        return None

    async def override_user_auth():
        return user

    _fastapp.dependency_overrides[get_master_async_session] = override_master_session
    _fastapp.dependency_overrides[rate_limit_login] = override_rate_limit_login
    _fastapp.dependency_overrides[rate_limit_password_reset] = override_rate_limit_password_reset
    _fastapp.dependency_overrides[user_auth] = override_user_auth

    with TestClient(asgi_app) as c:
        yield {
            "client": c,
            "supabase_client": supabase_client,
            "email_service": email_service,
            "master_session": master_session,
            "user": user,
        }
