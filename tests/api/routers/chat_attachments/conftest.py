import importlib
from uuid import uuid4

import fakeredis
import pytest
from fastapi.testclient import TestClient

from tests.api.routers.mocks import FakeUploadService


@pytest.fixture(scope="function")
def dependency_overrides(client, test_user_ids):
    """
    Fixture to override dependencies for the chat attachment upload tests.
    Must run AFTER client fixture which reloads app_state.
    """
    # Import fresh app after client fixture has reloaded app_state
    from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
    from src.api.routers.chat_attachments.upload_handler import get_upload_service
    from src.infra.utils.app_state import fastapp as _fastapp

    fake_service = FakeUploadService(test_user_ids["organisation_id"], test_user_ids["user_id"])

    def _override_user():
        return UserIDs(
            user_id=test_user_ids["user_id"],
            organisation_id=test_user_ids["organisation_id"],
            user_name=test_user_ids.get("user_name", "Test User"),
            user_email=test_user_ids.get("user_email", "test@example.com"),
        )

    async def _override_service():
        try:
            yield fake_service
        finally:
            await fake_service.close()

    _fastapp.dependency_overrides[get_current_user] = _override_user
    _fastapp.dependency_overrides[get_upload_service] = _override_service
    yield fake_service
    _fastapp.dependency_overrides = {}


@pytest.fixture(scope="function")
def access_test_data():
    """Create test data for access handler tests."""
    org_id = uuid4()
    user_id = uuid4()
    return {
        "user_id": user_id,
        "organisation_id": org_id,
    }


@pytest.fixture(scope="function")
def access_client(mocker, access_test_data):
    """Create a TestClient with dependencies for access handler tests."""
    import src.api.fastapi_app as fastapi_app
    import src.infra.utils.app_state as app_state

    importlib.reload(app_state)
    importlib.reload(fastapi_app)

    user_ids = access_test_data
    fake_service = FakeUploadService(user_ids["organisation_id"], user_ids["user_id"])

    # Patch Redis client
    mocker.patch(
        "src.infra.messaging.redis_client.get_redis_client",
        return_value=fakeredis.FakeRedis(),
    )

    # Build ASGI app
    asgi_app = fastapi_app.create_fastapi_app()

    from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
    from src.api.dependencies.rate_limit import rate_limit_user_read
    from src.api.routers.chat_attachments.access_handler import get_upload_service
    from src.infra.utils.app_state import fastapp as _fastapp

    def _override_user():
        return UserIDs(
            user_id=user_ids["user_id"],
            organisation_id=user_ids["organisation_id"],
            user_name="Test User",
            user_email="test@example.com",
        )

    async def _override_service():
        yield fake_service

    async def _override_rate_limit():
        return None

    _fastapp.dependency_overrides[get_current_user] = _override_user
    _fastapp.dependency_overrides[get_upload_service] = _override_service
    _fastapp.dependency_overrides[rate_limit_user_read] = _override_rate_limit

    with TestClient(asgi_app) as c:
        yield {
            "client": c,
            "service": fake_service,
            "user_ids": user_ids,
        }
