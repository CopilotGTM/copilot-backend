from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.service.crm.internal.internal_crm_user_service import InternalCRMUserServiceError
from src.service.crm.utils import crm_user_identity
from src.service.crm.utils.crm_user_identity import (
    CRMUserIdentityResolutionError,
    ensure_app_user_crm_identity,
)


@pytest.mark.asyncio
async def test_ensure_app_user_crm_identity_uses_shared_internal_service(monkeypatch) -> None:
    organisation_id = uuid4()
    app_user_id = uuid4()
    actor_user_id = uuid4()
    org_session = SimpleNamespace()
    master_session = SimpleNamespace()
    crm_user = SimpleNamespace(user_id=app_user_id)
    calls = []

    class FakeInternalCRMUserService:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        async def ensure_crm_user(self, user_id):
            calls.append({"ensure_user_id": user_id})
            return crm_user

    monkeypatch.setattr(
        crm_user_identity,
        "InternalCRMUserService",
        FakeInternalCRMUserService,
    )

    resolved = await ensure_app_user_crm_identity(
        org_session=org_session,  # type: ignore[arg-type]
        master_session=master_session,  # type: ignore[arg-type]
        organisation_id=organisation_id,
        app_user_id=app_user_id,
        actor_user_id=actor_user_id,
    )

    assert resolved is crm_user
    assert calls == [
        {
            "org_session": org_session,
            "master_session": master_session,
            "organisation_id": organisation_id,
            "actor_user_id": actor_user_id,
        },
        {"ensure_user_id": app_user_id},
    ]


@pytest.mark.asyncio
async def test_ensure_app_user_crm_identity_translates_internal_errors(monkeypatch) -> None:
    class FakeInternalCRMUserService:
        def __init__(self, **_kwargs):
            pass

        async def ensure_crm_user(self, _user_id):
            raise InternalCRMUserServiceError("User not in organisation", 403)

    monkeypatch.setattr(
        crm_user_identity,
        "InternalCRMUserService",
        FakeInternalCRMUserService,
    )

    with pytest.raises(CRMUserIdentityResolutionError) as exc_info:
        await ensure_app_user_crm_identity(
            org_session=SimpleNamespace(),  # type: ignore[arg-type]
            master_session=SimpleNamespace(),  # type: ignore[arg-type]
            organisation_id=uuid4(),
            app_user_id=uuid4(),
        )

    assert exc_info.value.message == "User not in organisation"
    assert exc_info.value.status_code == 403
