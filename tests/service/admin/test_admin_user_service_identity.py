from uuid import UUID, uuid4

import pytest
from src.domain.models.admin_user_models import CreateUserRequest
from src.domain.models.master_tables import Organisation, User, UserAppRole
from src.service.admin import admin_user_service as service_module
from src.service.admin.admin_user_service import AdminUserService, UserIdentityConflictError


class _RowsResult:
    def __init__(self, rows: list[tuple[UUID]]):
        self._rows = rows

    def all(self) -> list[tuple[UUID]]:
        return self._rows


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return self

    def first(self):
        return self._value


class _FakeOrgSession:
    def __init__(self, rows: list[tuple[UUID]]):
        self._rows = rows

    async def execute(self, _stmt):
        return _RowsResult(self._rows)


class _FakeMasterSession:
    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id
        self.added = []
        self.committed = False

    async def get(self, model, identity):
        if model is Organisation and identity == self.organisation_id:
            return Organisation(
                organisation_id=self.organisation_id,
                organisation_name="Test Org",
                owner_email="owner@example.com",
                owner_first_name="Owner",
                owner_last_name="User",
            )
        if model is User:
            return None
        return None

    async def execute(self, _stmt):
        return _ScalarResult(None)

    def add(self, item):
        self.added.append(item)

    async def commit(self):
        self.committed = True

    async def refresh(self, _item):
        return None

    async def close(self):
        return None


def _patch_org_session(monkeypatch, rows: list[tuple[UUID]]) -> None:
    async def fake_acquire_org_async_session(_organisation_id: UUID):
        yield _FakeOrgSession(rows)

    monkeypatch.setattr(service_module, "acquire_org_async_session", fake_acquire_org_async_session)


@pytest.mark.asyncio
async def test_resolve_crm_user_id_reuses_single_active_email_match(monkeypatch):
    crm_user_id = uuid4()
    _patch_org_session(monkeypatch, [(crm_user_id,), (crm_user_id,)])

    service = AdminUserService(_FakeMasterSession(uuid4()))

    resolved = await service._resolve_crm_user_id_for_new_user(
        organisation_id=uuid4(),
        email="  Arun.Prasadam@Exotel.com ",
    )

    assert resolved == crm_user_id


@pytest.mark.asyncio
async def test_resolve_crm_user_id_rejects_ambiguous_email_match(monkeypatch):
    _patch_org_session(monkeypatch, [(uuid4(),), (uuid4(),)])

    service = AdminUserService(_FakeMasterSession(uuid4()))

    with pytest.raises(UserIdentityConflictError):
        await service._resolve_crm_user_id_for_new_user(
            organisation_id=uuid4(),
            email="arun.prasadam@exotel.com",
        )


@pytest.mark.asyncio
async def test_create_user_reuses_crm_user_id_before_public_insert(monkeypatch):
    organisation_id = uuid4()
    crm_user_id = uuid4()
    master_session = _FakeMasterSession(organisation_id)
    _patch_org_session(monkeypatch, [(crm_user_id,)])
    monkeypatch.setattr(service_module.user_auth, "generate_temporary_password", lambda: "Temp123!")
    monkeypatch.setattr(
        service_module.user_auth,
        "create_supabase_user",
        lambda **_kwargs: "supabase-user-id",
    )

    service = AdminUserService(master_session)
    result = await service.create_user(
        admin_id=uuid4(),
        user_data=CreateUserRequest(
            first_name="Arun",
            last_name="Prasadam",
            email="arun.prasadam@exotel.com",
            organisation_id=organisation_id,
            user_role=UserAppRole.MEMBER,
            organisation_role="member",
        ),
    )

    created_user = next(item for item in master_session.added if isinstance(item, User))
    assert created_user.user_id == crm_user_id
    assert result.user_id == crm_user_id
    assert master_session.committed is True
