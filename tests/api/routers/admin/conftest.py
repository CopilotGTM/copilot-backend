"""Fixtures and fake services for Admin API tests."""

import importlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import fakeredis
import pytest
from fastapi.testclient import TestClient

# =============================================================================
# Fake Models
# =============================================================================


class FakeSuperAdmin:
    """Fake SuperAdmin model for testing."""

    def __init__(
        self,
        admin_id: UUID | None = None,
        email: str = "admin@example.com",
        first_name: str = "Admin",
        last_name: str = "User",
        role: str = "SUPER_ADMIN",
        status: str = "ACTIVE",
        supabase_admin_id: str | None = None,
    ):
        self.admin_id = admin_id or uuid4()
        self.email = email
        self.first_name = first_name
        self.last_name = last_name
        self.role = role
        self.status = status
        self.supabase_admin_id = supabase_admin_id or str(uuid4())
        self.created_at = datetime.now(UTC)
        self.last_login_at = datetime.now(UTC)


class FakeOrganisationData:
    """Fake Organisation data for testing."""

    def __init__(
        self,
        organisation_id: UUID | None = None,
        organisation_name: str = "Test Org",
        status: str = "approved",  # OrganisationStatus is lowercase
        country_code: str = "US",
    ):
        self.organisation_id = organisation_id or uuid4()
        self.organisation_name = organisation_name
        self.status = status
        self.country_code = country_code
        self.created_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)


class FakeUserData:
    """Fake User data for testing."""

    def __init__(
        self,
        user_id: UUID | None = None,
        email: str = "user@example.com",
        first_name: str = "Test",
        last_name: str = "User",
        organisation_id: UUID | None = None,
        user_role: str = "member",
        organisation_role: str = "member",
        status: str = "ACTIVE",  # SupabaseUserStatus is UPPERCASE
        admin_status: str = "approved",  # AdminUserStatus is lowercase
        authentication_method: str = "password",
        temp_password: str | None = None,
    ):
        self.user_id = user_id or uuid4()
        self.email = email
        self.first_name = first_name
        self.last_name = last_name
        self.organisation_id = organisation_id or uuid4()
        self.user_role = user_role
        self.organisation_role = organisation_role
        self.job_title = None
        self.status = status
        self.admin_status = admin_status
        self.authentication_method = authentication_method
        self.temp_password = temp_password
        self.created_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)
        self.approved_at = None
        self.approved_by = None


# =============================================================================
# Fake Services
# =============================================================================


class FakeAdminAuthService:
    """Fake admin auth service for testing."""

    def __init__(self):
        self._admins: dict[str, FakeSuperAdmin] = {}
        self._passwords: dict[str, str] = {}
        self._tokens: dict[str, str] = {}  # token_hash -> admin_id
        self._refresh_tokens: dict[str, str] = {}  # refresh_token -> admin_id

    def add_admin(
        self,
        email: str = "admin@example.com",
        password: str = "password123",
        role: str = "super_admin",
    ) -> FakeSuperAdmin:
        from src.domain.models.master_tables import AdminRole

        admin = FakeSuperAdmin(
            email=email,
            role=AdminRole.SUPER_ADMIN if role == "super_admin" else AdminRole.ADMIN,
        )
        self._admins[email] = admin
        self._passwords[email] = password
        return admin

    def add_recovery_token(self, token_hash: str, email: str):
        self._tokens[token_hash] = email

    def add_refresh_token(self, refresh_token: str, email: str):
        self._refresh_tokens[refresh_token] = email

    async def login(self, email: str, password: str):
        from src.service.admin.admin_auth_service import (
            ActiveLoginResult,
            AdminNotFoundError,
            InvalidCredentialsError,
        )

        admin = self._admins.get(email)
        if not admin:
            raise AdminNotFoundError("Admin not found")
        if self._passwords.get(email) != password:
            raise InvalidCredentialsError("Invalid credentials")

        tokens = {
            "access_token": f"access_{uuid4()}",
            "refresh_token": f"refresh_{uuid4()}",
        }
        # ActiveLoginResult expects a SuperAdmin-like object with the right attributes
        return ActiveLoginResult(
            super_admin=admin,
            tokens=tokens,
            email_verified=True,
        )

    async def reset_password(
        self, token_hash: str, new_password: str, confirm_password: str, action: str
    ):
        from src.service.admin.admin_auth_service import (
            InvalidRecoveryTokenError,
            PasswordMismatchError,
        )

        if new_password != confirm_password:
            raise PasswordMismatchError("Passwords do not match")
        if token_hash not in self._tokens:
            raise InvalidRecoveryTokenError("Invalid recovery token")
        return True

    async def forgot_password(self, email: str):
        return True

    async def logout(self, current_admin, logout_email, global_logout, bearer_token):
        if global_logout:
            return "Successfully logged out from all sessions"
        return "Successfully logged out"

    async def change_password(self, current_admin, current_password, new_password, bearer_token):
        from src.service.admin.admin_auth_service import InvalidCredentialsError

        if self._passwords.get(current_admin.email) != current_password:
            raise InvalidCredentialsError("Current password is incorrect")
        self._passwords[current_admin.email] = new_password
        return True

    async def close(self):
        pass


class FakeAdminOrganisationService:
    """Fake admin organisation service for testing."""

    def __init__(self):
        self._organisations: dict[UUID, FakeOrganisationData] = {}
        self._jobs: dict[UUID, dict] = {}
        self._history: list[dict] = []

    def add_organisation(
        self,
        organisation_id: UUID | None = None,
        organisation_name: str = "Test Org",
        status: str = "approved",
    ) -> FakeOrganisationData:
        org = FakeOrganisationData(
            organisation_id=organisation_id,
            organisation_name=organisation_name,
            status=status,
        )
        self._organisations[org.organisation_id] = org
        return org

    def _to_list_response(self, org: FakeOrganisationData):
        """Convert fake org to ListOrganizationResponse."""
        from src.domain.models.admin_org_models import ListOrganizationResponse

        return ListOrganizationResponse(
            organisation_id=org.organisation_id,
            organisation_name=org.organisation_name,
            is_active=org.status == "ACTIVE",
            status=org.status,
            created_at=org.created_at,
            updated_at=org.updated_at,
        )

    async def list_organisations(self, limit: int = 20, cursor: str | None = None):
        from src.domain.models.admin_org_models import PaginationOrganizations

        orgs = list(self._organisations.values())[:limit]
        org_responses = [self._to_list_response(o) for o in orgs]
        return PaginationOrganizations(organizations=org_responses, cursor=None)

    async def get_organisation(self, organisation_id: UUID):
        from src.service.admin.admin_organisation_service import OrganisationNotFoundError

        org = self._organisations.get(organisation_id)
        if not org:
            raise OrganisationNotFoundError("Organisation not found")
        return self._to_list_response(org)

    async def update_organisation(self, organisation_id: UUID, admin_id: UUID, update_data: dict):
        from src.service.admin.admin_organisation_service import OrganisationNotFoundError

        org = self._organisations.get(organisation_id)
        if not org:
            raise OrganisationNotFoundError("Organisation not found")
        for key, value in update_data.items():
            if hasattr(org, key):
                setattr(org, key, value)
        org.updated_at = datetime.now(UTC)
        return self._to_list_response(org)

    async def update_organisation_status(
        self, organisation_id: UUID, admin_id: UUID, new_status, note: str | None = None
    ):
        from src.domain.models.admin_org_models import UpdateOrganisationStatusResponse
        from src.service.admin.admin_organisation_service import OrganisationNotFoundError

        org = self._organisations.get(organisation_id)
        if not org:
            raise OrganisationNotFoundError("Organisation not found")
        status_value = new_status.value if hasattr(new_status, "value") else new_status
        org.status = status_value
        org.updated_at = datetime.now(UTC)
        return UpdateOrganisationStatusResponse(
            organisation_id=organisation_id,
            status=status_value,
            updated_at=org.updated_at,
            approved_at=org.updated_at if status_value == "approved" else None,
            approved_by=admin_id if status_value == "approved" else None,
        )

    async def get_all_organisations_history(self, limit: int = 20, cursor: str | None = None):
        from src.domain.models.admin_org_models import PaginatedOrganisationActionHistoryResponse

        return PaginatedOrganisationActionHistoryResponse(history=[], cursor=None)

    async def get_organisation_history(
        self, organisation_id: UUID, limit: int = 20, cursor: str | None = None
    ):
        from src.domain.models.admin_org_models import PaginatedOrganisationActionHistoryResponse
        from src.service.admin.admin_organisation_service import OrganisationNotFoundError

        org = self._organisations.get(organisation_id)
        if not org:
            raise OrganisationNotFoundError("Organisation not found")
        return PaginatedOrganisationActionHistoryResponse(history=[], cursor=None)

    async def create_job(self, job_type, admin_id, organisation_id, request_payload):
        from src.api.schemas.admin_job_models import OrganisationJobStatus, OrganisationJobType

        job_id = uuid4()
        organisation_id = organisation_id or uuid4()
        # Keep enum object if passed, otherwise convert to enum
        job_type_enum = (
            job_type if isinstance(job_type, OrganisationJobType) else OrganisationJobType(job_type)
        )
        self._jobs[job_id] = {
            "job_id": job_id,
            "job_type": job_type_enum,
            "status": OrganisationJobStatus.PENDING,
        }
        # Return a simple object with the expected attributes (using enums)
        return type(
            "JobResult",
            (),
            {
                "job_id": job_id,
                "job_type": job_type_enum,
                "status": OrganisationJobStatus.PENDING,
                "organisation_id": organisation_id,
            },
        )()

    async def close(self):
        pass


class FakeAdminUserService:
    """Fake admin user service for testing."""

    def __init__(self):
        self._users: dict[UUID, FakeUserData] = {}
        self._history: list[dict] = []

    def add_user(
        self,
        user_id: UUID | None = None,
        email: str = "user@example.com",
        organisation_id: UUID | None = None,
        user_role: str = "member",
        admin_status: str = "approved",
    ) -> FakeUserData:
        user = FakeUserData(
            user_id=user_id,
            email=email,
            organisation_id=organisation_id or uuid4(),
            user_role=user_role,
            admin_status=admin_status,
        )
        self._users[user.user_id] = user
        return user

    def _to_user_response(self, user: FakeUserData):
        """Convert fake user to UserResponse."""
        from src.domain.models.admin_user_models import UserResponse
        from src.domain.models.master_tables import AdminUserStatus, SupabaseUserStatus

        return UserResponse(
            user_id=user.user_id,
            first_name=user.first_name,
            last_name=user.last_name,
            email=user.email,
            organisation_id=user.organisation_id,
            organisation_role=user.organisation_role,
            user_role=user.user_role,
            status=SupabaseUserStatus(user.status) if isinstance(user.status, str) else user.status,
            admin_status=AdminUserStatus(user.admin_status)
            if isinstance(user.admin_status, str)
            else user.admin_status,
            authentication_method=user.authentication_method,
            created_at=user.created_at,
            updated_at=user.updated_at,
        )

    async def create_user(self, admin_id: UUID, user_data):
        from src.service.admin.admin_user_service import UserAlreadyExistsError

        # Check for duplicate
        for user in self._users.values():
            if user.email == user_data.email:
                raise UserAlreadyExistsError("User already exists")

        user = FakeUserData(
            email=user_data.email,
            first_name=user_data.first_name,
            last_name=user_data.last_name,
            organisation_id=user_data.organisation_id,
            user_role=user_data.user_role.value
            if hasattr(user_data.user_role, "value")
            else user_data.user_role,
            organisation_role=user_data.organisation_role,
            authentication_method=user_data.authentication_method,
            temp_password=(
                "TemporaryPassword123!"
                if getattr(
                    user_data.authentication_method, "value", user_data.authentication_method
                )
                == "password"
                else None
            ),
        )
        self._users[user.user_id] = user
        return user  # create_user handler creates its own CreateUserResponse

    async def get_user(self, user_id: UUID):
        from src.service.admin.admin_user_service import UserNotFoundError

        user = self._users.get(user_id)
        if not user:
            raise UserNotFoundError("User not found")
        return self._to_user_response(user)

    async def list_users(
        self, limit: int = 20, cursor: str | None = None, organisation_id: UUID | None = None
    ):
        from src.domain.models.admin_user_models import PaginatedUsers

        users = list(self._users.values())
        if organisation_id:
            users = [u for u in users if u.organisation_id == organisation_id]
        user_responses = [self._to_user_response(u) for u in users[:limit]]
        return PaginatedUsers(users=user_responses, cursor=None)

    async def update_user_admin_status(
        self, user_id: UUID, admin_id: UUID, new_admin_status, note: str | None = None
    ):
        from src.domain.models.master_tables import AdminUserStatus
        from src.service.admin.admin_user_service import UserNotFoundError

        user = self._users.get(user_id)
        if not user:
            raise UserNotFoundError("User not found")
        # Store the enum object, not its string value
        user.admin_status = (
            new_admin_status
            if hasattr(new_admin_status, "value")
            else AdminUserStatus(new_admin_status)
        )
        user.updated_at = datetime.now(UTC)
        status_value = (
            new_admin_status.value if hasattr(new_admin_status, "value") else new_admin_status
        )
        if status_value == "approved":
            user.approved_at = datetime.now(UTC)
            user.approved_by = admin_id
        return user  # Handler creates UpdateUserStatusResponse from user attributes

    async def list_user_action_history(
        self, organisation_id: UUID, limit: int = 20, cursor: str | None = None
    ):
        from src.domain.models.admin_user_models import PaginatedUserActionHistoryResponse

        return PaginatedUserActionHistoryResponse(history=[], cursor=None)

    async def update_user(self, user_id: UUID, admin_id: UUID, user_data):
        from src.service.admin.admin_user_service import UserNotFoundError

        user = self._users.get(user_id)
        if not user:
            raise UserNotFoundError("User not found")
        if hasattr(user_data, "first_name") and user_data.first_name:
            user.first_name = user_data.first_name
        if hasattr(user_data, "last_name") and user_data.last_name:
            user.last_name = user_data.last_name
        if user_data.user_role is not None:
            user.user_role = user_data.user_role.value
        user.updated_at = datetime.now(UTC)
        return self._to_user_response(user)

    async def close(self):
        pass


class FakeSupabaseAdminClient:
    """Fake Supabase admin client for testing."""

    def __init__(self):
        self._refresh_tokens: dict[str, str] = {}

    def add_refresh_token(self, refresh_token: str, admin_id: str):
        self._refresh_tokens[refresh_token] = admin_id

    def refresh_access_token(self, refresh_token: str) -> dict:
        if refresh_token not in self._refresh_tokens:
            return {"session": {}}
        return {
            "session": {
                "access_token": f"new_access_{uuid4()}",
                "refresh_token": f"new_refresh_{uuid4()}",
            }
        }


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(scope="function")
def admin_test_data():
    """Create test data for admin tests."""
    admin_id = uuid4()
    return {
        "admin_id": admin_id,
        "admin_email": "admin@example.com",
    }


@pytest.fixture(scope="function")
def admin_auth_client(mocker, admin_test_data):
    """Create a TestClient with dependencies for admin auth handler tests."""
    import src.api.fastapi_app as fastapi_app
    import src.infra.utils.app_state as app_state

    importlib.reload(app_state)
    importlib.reload(fastapi_app)

    fake_auth_service = FakeAdminAuthService()
    fake_supabase = FakeSupabaseAdminClient()

    # Add default admin
    admin = fake_auth_service.add_admin(
        email="admin@example.com",
        password="correctpassword",
        role="SUPER_ADMIN",
    )
    fake_auth_service.add_recovery_token("valid_recovery_token", "admin@example.com")
    fake_supabase.add_refresh_token("valid_refresh_token", str(admin.admin_id))

    # Patch Redis client
    mocker.patch(
        "src.infra.messaging.redis_client.get_redis_client",
        return_value=fakeredis.FakeRedis(),
    )

    # Patch Supabase client
    mocker.patch(
        "src.api.routers.admin.admin_auth_handler.supabase_admin_client",
        fake_supabase,
    )

    # Build ASGI app
    asgi_app = fastapi_app.create_fastapi_app()

    from src.api.dependencies.rate_limit import rate_limit_admin_login, rate_limit_password_reset
    from src.api.routers.admin.admin_auth_handler import get_admin_auth_service
    from src.domain.models.master_tables import AdminRole
    from src.infra.auth.admin_auth import admin_auth
    from src.infra.utils.app_state import fastapp as _fastapp

    async def _override_auth_service():
        yield fake_auth_service

    async def _override_rate_limit():
        return None

    def _override_admin_auth():
        return FakeSuperAdmin(
            admin_id=admin_test_data["admin_id"],
            email=admin_test_data["admin_email"],
            role=AdminRole.SUPER_ADMIN,
        )

    _fastapp.dependency_overrides[get_admin_auth_service] = _override_auth_service
    _fastapp.dependency_overrides[rate_limit_admin_login] = _override_rate_limit
    _fastapp.dependency_overrides[rate_limit_password_reset] = _override_rate_limit
    _fastapp.dependency_overrides[admin_auth] = _override_admin_auth

    with TestClient(asgi_app) as c:
        yield {
            "client": c,
            "auth_service": fake_auth_service,
            "supabase_client": fake_supabase,
            "admin": admin,
        }


@pytest.fixture(scope="function")
def admin_org_client(mocker, admin_test_data):
    """Create a TestClient with dependencies for admin organisations handler tests."""
    import src.api.fastapi_app as fastapi_app
    import src.infra.utils.app_state as app_state

    importlib.reload(app_state)
    importlib.reload(fastapi_app)

    fake_service = FakeAdminOrganisationService()

    mocker.patch.object(fastapi_app, "validate_current_shared_tenant_runtime_startup")
    mocker.patch.object(
        fastapi_app,
        "validate_current_shared_tenant_runtime_password",
        new=AsyncMock(),
    )

    # Patch Redis client
    mocker.patch(
        "src.infra.messaging.redis_client.get_redis_client",
        return_value=fakeredis.FakeRedis(),
    )
    mocker.patch("src.api.fastapi_app.initialize_redis_pool", return_value=None)
    mocker.patch("src.api.fastapi_app.close_redis_pool", return_value=None)
    mocker.patch("src.api.fastapi_app.initialize_async_redis_pool", new=AsyncMock())
    mocker.patch("src.api.fastapi_app.close_async_redis_pool", new=AsyncMock())
    mocker.patch("src.api.fastapi_app.async_init_otel", new=AsyncMock())
    mocker.patch("src.api.fastapi_app.init_sentry", return_value=None)
    mocker.patch(
        "src.infra.auth.supabase_admin_client.supabase_admin_client.initialize",
        new=AsyncMock(),
    )
    mocker.patch(
        "src.infra.auth.supabase_user_client.supabase_user_client.initialize",
        new=AsyncMock(),
    )
    mocker.patch(
        "src.infra.security.token_revocation.token_revocation_service.initialize",
        new=AsyncMock(),
    )

    # Patch task senders to avoid actual message queue calls (must be async)
    async def fake_send_task(*args, **kwargs):
        return None

    mocker.patch(
        "src.api.routers.admin.organizations_handler.send_organization_create_task",
        side_effect=fake_send_task,
    )

    # Build ASGI app
    asgi_app = fastapi_app.create_fastapi_app()

    from src.api.dependencies.rate_limit import rate_limit_admin_read
    from src.api.routers.admin.organizations_handler import get_admin_organisation_service
    from src.domain.models.master_tables import AdminRole
    from src.infra.auth.admin_auth import admin_auth
    from src.infra.utils.app_state import fastapp as _fastapp

    async def _override_service():
        yield fake_service

    async def _override_rate_limit():
        return None

    def _override_admin_auth():
        return FakeSuperAdmin(
            admin_id=admin_test_data["admin_id"],
            email=admin_test_data["admin_email"],
            role=AdminRole.SUPER_ADMIN,
        )

    _fastapp.dependency_overrides[get_admin_organisation_service] = _override_service
    _fastapp.dependency_overrides[rate_limit_admin_read] = _override_rate_limit
    _fastapp.dependency_overrides[admin_auth] = _override_admin_auth

    with TestClient(asgi_app) as c:
        yield {
            "client": c,
            "service": fake_service,
            "admin_id": admin_test_data["admin_id"],
        }


@pytest.fixture(scope="function")
def admin_user_client(mocker, admin_test_data):
    """Create a TestClient with dependencies for admin users handler tests."""
    import src.api.fastapi_app as fastapi_app
    import src.infra.utils.app_state as app_state

    importlib.reload(app_state)
    importlib.reload(fastapi_app)

    fake_service = FakeAdminUserService()

    # Patch Redis client
    mocker.patch(
        "src.infra.messaging.redis_client.get_redis_client",
        return_value=fakeredis.FakeRedis(),
    )
    mocker.patch("src.api.fastapi_app.initialize_redis_pool", return_value=None)
    mocker.patch("src.api.fastapi_app.close_redis_pool", return_value=None)
    mocker.patch("src.api.fastapi_app.initialize_async_redis_pool", new=AsyncMock())
    mocker.patch("src.api.fastapi_app.close_async_redis_pool", new=AsyncMock())
    mocker.patch("src.api.fastapi_app.async_init_otel", new=AsyncMock())
    mocker.patch("src.api.fastapi_app.init_sentry", return_value=None)
    mocker.patch(
        "src.infra.auth.supabase_admin_client.supabase_admin_client.initialize",
        new=AsyncMock(),
    )
    mocker.patch(
        "src.infra.auth.supabase_user_client.supabase_user_client.initialize",
        new=AsyncMock(),
    )
    mocker.patch(
        "src.infra.security.token_revocation.token_revocation_service.initialize",
        new=AsyncMock(),
    )

    # Build ASGI app
    asgi_app = fastapi_app.create_fastapi_app()

    from src.api.dependencies.rate_limit import rate_limit_admin_read
    from src.api.routers.admin.users_handler import get_admin_user_service
    from src.domain.models.master_tables import AdminRole
    from src.infra.auth.admin_auth import admin_auth
    from src.infra.utils.app_state import fastapp as _fastapp

    async def _override_service():
        yield fake_service

    async def _override_rate_limit():
        return None

    def _override_admin_auth():
        return FakeSuperAdmin(
            admin_id=admin_test_data["admin_id"],
            email=admin_test_data["admin_email"],
            role=AdminRole.SUPER_ADMIN,
        )

    _fastapp.dependency_overrides[get_admin_user_service] = _override_service
    _fastapp.dependency_overrides[rate_limit_admin_read] = _override_rate_limit
    _fastapp.dependency_overrides[admin_auth] = _override_admin_auth

    with TestClient(asgi_app) as c:
        yield {
            "client": c,
            "service": fake_service,
            "admin_id": admin_test_data["admin_id"],
        }
