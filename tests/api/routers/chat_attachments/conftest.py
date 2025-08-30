import pytest

from src.api.dependencies.organisation_dependencies import get_current_user, UserIDs
from src.api.routers.chat_attachments.upload_handler import get_upload_service
from src.infra.utils.app_state import fastapp as app
from tests.api.routers.mocks import FakeUploadService


@pytest.fixture(scope="module")
def dependency_overrides(test_user_ids):
    """
    Fixture to override dependencies for the chat attachment tests.
    It yields a fake upload service that preserves state across test functions.
    """
    # Provide a single service instance per test module to preserve state across calls
    fake_service = FakeUploadService(test_user_ids["organisation_id"], test_user_ids["user_id"])

    def _override_user():
        return UserIDs(user_id=test_user_ids["user_id"], organisation_id=test_user_ids["organisation_id"])

    async def _override_service():
        try:
            yield fake_service
        finally:
            await fake_service.close()

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_upload_service] = _override_service
    yield
    app.dependency_overrides = {}