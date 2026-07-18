"""Admin-only chat attachment catalogue endpoint contracts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import get_org_async_session
from src.api.dependencies.rate_limit import rate_limit_user_read
from src.domain.models.master_tables import UserAppRole
from src.infra.auth.user_auth import user_auth


def _override_catalog_dependencies(*, role: UserAppRole):
    organisation_id = uuid4()
    org_session = SimpleNamespace(execute=AsyncMock())
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    org_session.execute.return_value = result
    master_session = SimpleNamespace(execute=AsyncMock())

    async def _override_user():
        return SimpleNamespace(
            user_id=uuid4(),
            organisation_id=organisation_id,
            user_role=role,
        )

    async def _override_org_session():
        yield org_session

    async def _override_master_session():
        yield master_session

    async def _override_rate_limit():
        return None

    return {
        user_auth: _override_user,
        get_org_async_session: _override_org_session,
        get_master_async_session: _override_master_session,
        rate_limit_user_read: _override_rate_limit,
    }, org_session


def test_admin_lists_sent_audio_attachments(client) -> None:
    """An organisation admin can load the initially empty audio catalogue."""
    from src.infra.utils.app_state import fastapp

    overrides, org_session = _override_catalog_dependencies(role=UserAppRole.ADMIN)
    fastapp.dependency_overrides.update(overrides)

    response = client.get(
        "/users/chats/attachments",
        params={"media_type": "audio"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"items": [], "cursor": None}
    org_session.execute.assert_awaited_once()


def test_member_cannot_list_sent_audio_attachments(client) -> None:
    """A regular member cannot enumerate another user's sent audio."""
    from src.infra.utils.app_state import fastapp

    overrides, org_session = _override_catalog_dependencies(role=UserAppRole.MEMBER)
    fastapp.dependency_overrides.update(overrides)

    response = client.get(
        "/users/chats/attachments",
        params={"media_type": "audio"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Organisation admin access required"
    org_session.execute.assert_not_awaited()
