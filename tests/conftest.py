import importlib
import os
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

# Respect existing .env values; only set test ENV if not provided.
# This avoids overriding your configured credentials during tests.
load_dotenv(override=False)
os.environ.setdefault("ENV", "local")
os.environ.setdefault("VOICE_REALTIME_API_ENABLED", "true")

import fakeredis
from src.api.dependencies.organisation_dependencies import auth as auth_dependency
from src.api.dependencies.organisation_dependencies import get_org_session

# Use an in-memory SQLite database for testing
DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,  # ensure single shared connection so in-memory schema persists across sessions
)


def override_get_org_session():
    """Override the get_org_session dependency to use the test database."""
    with Session(engine) as session:
        yield session


def _fake_verify_session():
    # Return dict shaped like real auth.verify_session output
    return {
        "user_id": str(uuid4()),
        "organisation_id": str(uuid4()),
    }


# Note: We intentionally skip creating DB tables here because upload tests
# override the UploadService to avoid database usage. If a test needs DB,
# add a dedicated fixture in that test module.


def override_get_redis_client():
    """Override the get_redis_client dependency to use a fake redis client."""
    return fakeredis.FakeRedis()


@pytest.fixture(scope="function")
def client(mocker):
    """Create a TestClient against the standard application factory.

    We cannot pass a custom FastAPI instance because `create_fastapi_app` does not
    accept an override parameter. Instead, we patch dependencies directly before
    creating the ASGI app. This keeps changes test‑local and avoids touching src/.
    """
    # Reload app_state and fastapi_app to ensure a fresh FastAPI instance per test
    import src.api.fastapi_app as fastapi_app
    import src.infra.utils.app_state as app_state

    importlib.reload(app_state)
    importlib.reload(fastapi_app)

    # Patch Redis client used during app creation
    mocker.patch(
        "src.infra.messaging.redis_client.get_redis_client", return_value=fakeredis.FakeRedis()
    )
    mocker.patch("src.api.fastapi_app.initialize_redis_pool", return_value=None)
    mocker.patch("src.api.fastapi_app.close_redis_pool", return_value=None)
    mocker.patch("src.api.fastapi_app.initialize_async_redis_pool", new=AsyncMock())
    mocker.patch("src.api.fastapi_app.close_async_redis_pool", new=AsyncMock())
    mocker.patch("src.api.fastapi_app.async_init_otel", new=AsyncMock())
    mocker.patch(
        "src.api.routers.integrations.copilot_oauth_routes.app_config.APP_URL",
        "http://localhost:3000",
    )
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

    # Patch auth verification dependency (module path to the function used in router dependencies)
    mocker.patch(
        "src.api.dependencies.organisation_dependencies.auth.verify_session",
        side_effect=_fake_verify_session,
    )

    # Build ASGI app (Socket.IO wrapped)
    asgi_app = fastapi_app.create_fastapi_app()

    # Apply dependency overrides on the underlying fastapp (import inside to avoid circulars)
    from src.infra.utils.app_state import fastapp as _fastapp

    _fastapp.dependency_overrides[get_org_session] = override_get_org_session
    _fastapp.dependency_overrides[auth_dependency.verify_session] = _fake_verify_session

    with TestClient(asgi_app) as c:
        yield c


@pytest.fixture(scope="session")
def test_user_ids():
    """Pytest fixture to provide consistent user/org IDs for tests."""
    return {
        "user_id": uuid4(),
        "organisation_id": uuid4(),
    }
