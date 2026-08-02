from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from src.domain.models.master_tables import (
    AdminUserStatus,
    OrganisationStatus,
    SupabaseUserStatus,
    UserAuthenticationMethod,
)
from src.infra.auth.supabase_user_client import VerifiedSupabaseSession
from src.infra.auth.user_auth import UserAuth
from starlette.requests import Request


class _Result:
    def __init__(self, user):
        self.user = user

    def scalars(self):
        return self

    def first(self):
        return self.user


@pytest.mark.asyncio
async def test_user_auth_rejects_wrong_verified_session_method(monkeypatch) -> None:
    import src.infra.auth.user_auth as module

    organisation_id = uuid4()
    user = SimpleNamespace(
        organisation_id=organisation_id,
        authentication_method=UserAuthenticationMethod.GOOGLE_WORKSPACE,
        status=SupabaseUserStatus.ACTIVE,
        admin_status=AdminUserStatus.APPROVED,
    )
    organisation = SimpleNamespace(
        is_active=True,
        status=OrganisationStatus.APPROVED,
    )

    class _Session:
        async def execute(self, _statement):
            return _Result(user)

        async def get(self, _model, identity):
            return organisation if identity == organisation_id else None

        def expunge(self, _value):
            return None

    @asynccontextmanager
    async def session_scope():
        yield _Session()

    auth = UserAuth()
    monkeypatch.setattr(module, "master_async_session_scope", session_scope)
    monkeypatch.setattr(
        auth.supabase_user_client,
        "verify_session",
        lambda _token: VerifiedSupabaseSession(
            user={"id": "supabase-user"},
            claims={},
            supabase_user_id="supabase-user",
            authentication_method=UserAuthenticationMethod.PASSWORD,
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await auth.get_current_user(
            Request({"type": "http", "headers": [(b"authorization", b"Bearer a.b.c")]})
        )

    assert exc_info.value.detail == "GOOGLE_SIGN_IN_REQUIRED"
