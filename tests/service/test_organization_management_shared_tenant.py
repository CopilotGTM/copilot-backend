"""Clean-break contracts for organisation creation on the shared tenant schema."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from src.domain.models.master_tables import (
    Organisation,
    OrganisationActionHistory,
    OrganisationStatus,
)
from src.service.orgnisation import organization_management as org_management_module
from src.service.orgnisation.organization_management import OrganizationManagementService


class _ScalarResult:
    def __init__(self, organisation: Organisation | None) -> None:
        self.organisation = organisation

    def scalars(self) -> _ScalarResult:
        """Mirror the SQLAlchemy result chain used by the service."""
        return self

    def first(self) -> Organisation | None:
        """Return the configured existing organisation."""
        return self.organisation


class _MasterSession:
    def __init__(
        self,
        state: dict[str, Organisation],
        *,
        existing: Organisation | None = None,
    ) -> None:
        self.state = state
        self.existing = existing
        self.added: list[object] = []
        self.commits = 0

    async def execute(self, _statement) -> _ScalarResult:
        """Return the existing organisation for the name lookup."""
        return _ScalarResult(self.existing)

    def add(self, value: object) -> None:
        """Record added rows and expose the organisation to the next session."""
        self.added.append(value)
        if isinstance(value, Organisation):
            self.state["organisation"] = value

    async def commit(self) -> None:
        """Count the service's control-plane commits."""
        self.commits += 1

    async def refresh(self, _value: object) -> None:
        """Match the async session contract without database I/O."""
        return None

    async def get(self, _model, _identifier):  # noqa: ANN001
        """Return the organisation persisted by the first fake session."""
        return self.state.get("organisation")


def _install_master_sessions(
    monkeypatch,
    *,
    existing: Organisation | None = None,
) -> tuple[dict[str, Organisation], list[_MasterSession]]:
    state: dict[str, Organisation] = {}
    if existing is not None:
        state["organisation"] = existing
    sessions = [
        _MasterSession(state, existing=existing),
        _MasterSession(state),
    ]

    @asynccontextmanager
    async def fake_master_session_scope():
        """Yield each phase's isolated fake master session."""
        yield sessions.pop(0)

    monkeypatch.setattr(
        org_management_module,
        "master_async_session_scope",
        fake_master_session_scope,
    )
    return state, sessions


def _forbid_legacy_database_provisioning(
    monkeypatch,
    service: OrganizationManagementService,
) -> None:
    """Make the red test fail before any configured external database is touched."""

    def reject(*_args, **_kwargs):
        """Fail before any removed provisioning hook can touch infrastructure."""
        raise AssertionError("per-organisation database provisioning was called")

    monkeypatch.setattr(
        service,
        "_provision_tenant_database",
        reject,
        raising=False,
    )
    monkeypatch.setattr(
        service,
        "_store_password_in_key_vault",
        reject,
        raising=False,
    )


@pytest.mark.asyncio
async def test_create_organization_uses_master_and_existing_org_boundaries(
    monkeypatch,
) -> None:
    """Creation persists control data, then seeds defaults through the org session."""

    state, sessions = _install_master_sessions(monkeypatch)
    first_session, final_session = tuple(sessions)
    service = OrganizationManagementService()
    _forbid_legacy_database_provisioning(monkeypatch, service)
    setup_internal_crm = AsyncMock()
    provision_storage = Mock()
    monkeypatch.setattr(service, "_setup_internal_crm", setup_internal_crm)
    monkeypatch.setattr(
        service,
        "_provision_org_storage_container",
        provision_storage,
    )
    admin_id = uuid4()

    result = await service.create_organization(
        admin_id=admin_id,
        organisation_name="Acme",
        country="US",
        linked_urls=["https://www.linkedin.com/company/acme"],
        website_urls=["https://acme.example"],
    )

    organisation = state["organisation"]
    assert result == {"organization": organisation}
    assert organisation.status is OrganisationStatus.PENDING
    assert organisation.created_by_auth_user_id == str(admin_id)
    assert organisation.linked_urls == ["https://www.linkedin.com/company/acme"]
    assert organisation.website_urls == ["https://acme.example"]
    setup_internal_crm.assert_awaited_once_with(
        organisation.organisation_id,
        admin_id,
    )
    provision_storage.assert_called_once_with(organisation.organisation_id)
    assert first_session.commits == 1
    assert final_session.commits == 1
    assert any(isinstance(value, OrganisationActionHistory) for value in final_session.added)


@pytest.mark.asyncio
async def test_create_organization_retry_reuses_provisioning_record(monkeypatch) -> None:
    """A task retry must reuse the master UUID and idempotent tenant defaults."""

    admin_id = uuid4()
    existing = Organisation(
        organisation_id=uuid4(),
        organisation_name="Acme",
        status=OrganisationStatus.PROVISIONING,
        created_by_auth_user_id=str(admin_id),
        country="GB",
    )
    state, _sessions = _install_master_sessions(
        monkeypatch,
        existing=existing,
    )
    service = OrganizationManagementService()
    _forbid_legacy_database_provisioning(monkeypatch, service)
    setup_internal_crm = AsyncMock()
    monkeypatch.setattr(service, "_setup_internal_crm", setup_internal_crm)
    monkeypatch.setattr(
        service,
        "_provision_org_storage_container",
        Mock(),
    )

    result = await service.create_organization(
        admin_id=admin_id,
        organisation_name="Acme",
        country="US",
    )

    assert result["organization"] is existing
    assert state["organisation"].organisation_id == existing.organisation_id
    assert existing.country == "US"
    assert existing.status is OrganisationStatus.PENDING
    setup_internal_crm.assert_awaited_once_with(existing.organisation_id, admin_id)


@pytest.mark.asyncio
async def test_public_tenant_default_boundary_reuses_crm_and_storage_setup(
    monkeypatch,
) -> None:
    """Signup must call the same idempotent defaults as admin org creation."""

    service = OrganizationManagementService()
    setup_internal_crm = AsyncMock()
    provision_storage = Mock()
    monkeypatch.setattr(service, "_setup_internal_crm", setup_internal_crm)
    monkeypatch.setattr(
        service,
        "_provision_org_storage_container",
        provision_storage,
    )
    organisation_id = uuid4()
    user_id = uuid4()

    await service.setup_tenant_defaults(organisation_id, user_id)

    setup_internal_crm.assert_awaited_once_with(organisation_id, user_id)
    provision_storage.assert_called_once_with(organisation_id)


def test_organization_service_has_no_per_org_database_provisioning_api() -> None:
    """The clean break must leave no callable legacy database lifecycle."""

    service = OrganizationManagementService()
    for removed_name in (
        "_create_database_role_and_schema",
        "_run_organization_migrations",
        "_provision_tenant_database",
        "_grant_tenant_runtime_role_access",
        "_store_password_in_key_vault",
        "_delete_password_from_key_vault",
        "rotate_organization_password",
        "delete_organization",
    ):
        assert not hasattr(service, removed_name)
