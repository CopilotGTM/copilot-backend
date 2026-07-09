from uuid import UUID, uuid4

import pytest
from src.domain.models.admin_user_models import CreateUserRequest
from src.domain.models.master_tables import Organisation, User, UserAppRole
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.domain.models.organisation_tables.sync.integration_models import KnowledgeIntegration
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
        self.added = []
        self.flushed = False
        self.committed = False

    async def execute(self, _stmt):
        return _RowsResult(self._rows)

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        self.flushed = True

    async def refresh(self, _item):
        return None

    async def commit(self):
        self.committed = True


class _FakeMasterSession:
    def __init__(self, organisation_id: UUID, user_by_id: dict[UUID, User] | None = None):
        self.organisation_id = organisation_id
        self.user_by_id = user_by_id or {}
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
            if identity in self.user_by_id:
                return self.user_by_id[identity]
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


def _patch_org_session(
    monkeypatch, rows: list[tuple[UUID]], session: _FakeOrgSession | None = None
) -> _FakeOrgSession:
    org_session = session or _FakeOrgSession(rows)

    async def fake_acquire_org_async_session(_organisation_id: UUID):
        yield org_session

    monkeypatch.setattr(service_module, "acquire_org_async_session", fake_acquire_org_async_session)
    return org_session


def _patch_internal_setup(monkeypatch, integration_id: UUID) -> None:
    class FakeInternalCRMSetupService:
        def __init__(self, *_args, **_kwargs):
            pass

        async def setup_internal_crm(self, *, commit: bool = True):
            return type(
                "SetupResult",
                (),
                {
                    "integration": KnowledgeIntegration(
                        integration_id=integration_id,
                        organisation_id=uuid4(),
                        name="Copilot CRM",
                    )
                },
            )()

    monkeypatch.setattr(
        service_module,
        "InternalCRMSetupService",
        FakeInternalCRMSetupService,
    )


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
    _patch_internal_setup(monkeypatch, uuid4())
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


@pytest.mark.asyncio
async def test_create_user_rejects_crm_uuid_already_used_by_different_app_user(monkeypatch):
    organisation_id = uuid4()
    crm_user_id = uuid4()
    existing_user = User(
        user_id=crm_user_id,
        email="old.owner@example.com",
        first_name="Old",
        last_name="Owner",
        organisation_id=organisation_id,
        user_role=UserAppRole.MEMBER,
    )
    master_session = _FakeMasterSession(organisation_id, user_by_id={crm_user_id: existing_user})
    _patch_org_session(monkeypatch, [(crm_user_id,)])

    service = AdminUserService(master_session)

    with pytest.raises(UserIdentityConflictError) as exc_info:
        await service.create_user(
            admin_id=uuid4(),
            user_data=CreateUserRequest(
                first_name="New",
                last_name="Owner",
                email="new.owner@example.com",
                organisation_id=organisation_id,
                user_role=UserAppRole.MEMBER,
                organisation_role="member",
            ),
        )

    assert exc_info.value.error_code == "CRM_USER_ID_ALREADY_LINKED"
    assert "with a different email" in exc_info.value.message
    assert master_session.added == []


@pytest.mark.asyncio
async def test_create_user_creates_internal_crm_user_when_email_has_no_crm_match(monkeypatch):
    organisation_id = uuid4()
    integration_id = uuid4()
    org_session = _patch_org_session(monkeypatch, [])
    _patch_internal_setup(monkeypatch, integration_id)
    master_session = _FakeMasterSession(organisation_id)
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
            first_name="Nitesh",
            last_name="Yadav",
            email="nitesh@obbserv.com",
            organisation_id=organisation_id,
            user_role=UserAppRole.MEMBER,
            organisation_role="member",
        ),
    )

    created_user = next(item for item in master_session.added if isinstance(item, User))
    created_crm_user = next(item for item in org_session.added if isinstance(item, CRMUser))
    assert created_crm_user.user_id == created_user.user_id == result.user_id
    assert created_crm_user.organisation_id == organisation_id
    assert created_crm_user.integration_id == integration_id
    assert created_crm_user.email == "nitesh@obbserv.com"


@pytest.mark.asyncio
async def test_create_user_allows_public_email_provider_when_no_crm_match(monkeypatch):
    organisation_id = uuid4()
    integration_id = uuid4()
    org_session = _patch_org_session(monkeypatch, [])
    _patch_internal_setup(monkeypatch, integration_id)
    master_session = _FakeMasterSession(organisation_id)
    monkeypatch.setattr(service_module.user_auth, "generate_temporary_password", lambda: "Temp123!")
    monkeypatch.setattr(
        service_module.user_auth,
        "create_supabase_user",
        lambda **_kwargs: "supabase-user-id",
    )

    service = AdminUserService(master_session)
    await service.create_user(
        admin_id=uuid4(),
        user_data=CreateUserRequest(
            first_name="Gmail",
            last_name="User",
            email="Gmail.User@gmail.com",
            organisation_id=organisation_id,
            user_role=UserAppRole.MEMBER,
            organisation_role="member",
        ),
    )

    created_user = next(item for item in master_session.added if isinstance(item, User))
    created_crm_user = next(item for item in org_session.added if isinstance(item, CRMUser))
    assert created_user.email == "gmail.user@gmail.com"
    assert created_crm_user.email == "gmail.user@gmail.com"
