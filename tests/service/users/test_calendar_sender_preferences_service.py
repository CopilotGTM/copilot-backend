"""Tests for validating and storing preferred calendar accounts."""

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from src.domain.models.master_tables.super_admin_models import AdminUserStatus
from src.domain.models.master_tables.user_models import SupabaseUserStatus, User, UserAppRole
from src.service.users.calendar_sender_preferences_service import (
    CalendarSenderPreferencesService,
)
from src.service.users.user_preferences import preferred_calendar_sender

ORG_ID = UUID("11111111-1111-1111-1111-111111111111")
USER_ID = UUID("22222222-2222-2222-2222-222222222222")


def _user() -> User:
    """Create the authenticated master user updated by preference tests."""
    return User(
        user_id=USER_ID,
        organisation_id=ORG_ID,
        first_name="Saksham",
        last_name="Bhutani",
        email="saksham@contoso.com",
        user_role=UserAppRole.MEMBER,
        organisation_role="Sales",
        status=SupabaseUserStatus.ACTIVE,
        admin_status=AdminUserStatus.APPROVED,
    )


@pytest.mark.asyncio
async def test_preference_service_stores_only_connected_calendar() -> None:
    """A tenant-owned connected calendar may become the user's persisted default."""
    user = _user()
    org_result = MagicMock()
    org_result.scalar_one_or_none.return_value = uuid4()
    master_result = MagicMock()
    master_result.scalar_one_or_none.return_value = user
    org_session = MagicMock(execute=AsyncMock(return_value=org_result))
    master_session = MagicMock(
        execute=AsyncMock(return_value=master_result),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    service = CalendarSenderPreferencesService(
        master_session=master_session,
        org_session=org_session,
        organisation_id=ORG_ID,
        user_id=USER_ID,
    )

    result = await service.update(" Sales@Contoso.com ")

    assert result == "sales@contoso.com"
    assert preferred_calendar_sender(user.preferences) == "sales@contoso.com"
    master_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_preference_service_rejects_unconnected_sender() -> None:
    """An arbitrary email must not become a pointer to another user's credentials."""
    org_result = MagicMock()
    org_result.scalar_one_or_none.return_value = None
    service = CalendarSenderPreferencesService(
        master_session=MagicMock(),
        org_session=MagicMock(execute=AsyncMock(return_value=org_result)),
        organisation_id=ORG_ID,
        user_id=USER_ID,
    )

    with pytest.raises(HTTPException) as exc_info:
        await service.update("other@contoso.com")

    assert exc_info.value.status_code == 400
