"""Tests for HTTP-level CRM role guards."""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from src.api.dependencies.crm_record_access import require_crm_admin
from src.domain.models.master_tables.user_models import UserAppRole
from src.infra.auth.user_context import UserIDs


def _user(role: UserAppRole) -> UserIDs:
    return UserIDs(
        user_id=uuid4(),
        organisation_id=uuid4(),
        user_role=role,
    )


@pytest.mark.asyncio
async def test_admin_guard_rejects_member() -> None:
    """A normal member cannot invoke organization-wide CRM operations."""
    with pytest.raises(HTTPException) as exc_info:
        await require_crm_admin(_user(UserAppRole.MEMBER))

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_guard_accepts_admin() -> None:
    """An authenticated administrator satisfies the organization-wide guard."""
    assert await require_crm_admin(_user(UserAppRole.ADMIN)) is None
