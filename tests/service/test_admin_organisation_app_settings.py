from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from src.domain.models.admin_org_models import UpdateOrganisationAppSettingsRequest
from src.service.admin import admin_organisation_service as service_module
from src.service.admin.admin_organisation_service import AdminOrganisationService


class _MasterSession:
    def __init__(self, organisation):
        self.organisation = organisation
        self.committed = False

    async def get(self, _model, _organisation_id):
        return self.organisation

    def add(self, _obj):
        pass

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        pass


class _TenantSession:
    def __init__(self, has_deal: bool):
        self.has_deal = has_deal

    async def execute(self, _statement):
        return SimpleNamespace(
            scalar_one_or_none=lambda: uuid4() if self.has_deal else None,
        )


def _organisation(organisation_id: UUID, preferences: dict | None):
    return SimpleNamespace(
        organisation_id=organisation_id,
        organisation_name="Acme",
        is_active=True,
        status="approved",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        preferences=preferences,
    )


def _patch_tenant_session(monkeypatch: pytest.MonkeyPatch, *, has_deal: bool) -> None:
    async def acquire_org_async_session(
        _organisation_id: UUID,
    ) -> AsyncGenerator[_TenantSession, None]:
        yield _TenantSession(has_deal)

    monkeypatch.setattr(
        service_module,
        "acquire_org_async_session",
        acquire_org_async_session,
        raising=False,
    )


@pytest.mark.asyncio
async def test_rejects_crm_provider_change_after_any_deal_exists(monkeypatch: pytest.MonkeyPatch):
    organisation_id = uuid4()
    organisation = _organisation(
        organisation_id,
        {"crm": {"provider": "copilotgtm"}},
    )
    master_session = _MasterSession(organisation)
    _patch_tenant_session(monkeypatch, has_deal=True)

    service = AdminOrganisationService(master_session)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="CRM source cannot be changed"):
        await service.update_organisation_app_settings(
            organisation_id=organisation_id,
            admin_id=uuid4(),
            settings=UpdateOrganisationAppSettingsRequest(crm={"provider": "hubspot"}),
        )

    assert master_session.committed is False


@pytest.mark.asyncio
async def test_allows_channel_update_after_deals_exist(monkeypatch: pytest.MonkeyPatch):
    organisation_id = uuid4()
    organisation = _organisation(
        organisation_id,
        {"crm": {"provider": "copilotgtm"}},
    )
    master_session = _MasterSession(organisation)
    _patch_tenant_session(monkeypatch, has_deal=True)

    service = AdminOrganisationService(master_session)  # type: ignore[arg-type]

    await service.update_organisation_app_settings(
        organisation_id=organisation_id,
        admin_id=uuid4(),
        settings=UpdateOrganisationAppSettingsRequest(channels={"enabled": ["slack"]}),
    )

    assert master_session.committed is True
    assert organisation.preferences["channels"] == {"enabled": ["slack"]}


@pytest.mark.asyncio
async def test_updates_google_gmail_scope_preference_without_deal_lock(
    monkeypatch: pytest.MonkeyPatch,
):
    organisation_id = uuid4()
    organisation = _organisation(
        organisation_id,
        {
            "crm": {"provider": "copilotgtm"},
            "integrations": {"microsoft": {"admin_consent": {"granted": True}}},
        },
    )
    master_session = _MasterSession(organisation)
    _patch_tenant_session(monkeypatch, has_deal=True)

    service = AdminOrganisationService(master_session)  # type: ignore[arg-type]

    await service.update_organisation_app_settings(
        organisation_id=organisation_id,
        admin_id=uuid4(),
        settings=UpdateOrganisationAppSettingsRequest(
            integrations={"google": {"gmail_read_enabled": True}}
        ),
    )

    assert master_session.committed is True
    assert organisation.preferences["integrations"]["google"] == {"gmail_read_enabled": True}
    assert organisation.preferences["integrations"]["microsoft"] == {
        "admin_consent": {"granted": True}
    }


@pytest.mark.asyncio
async def test_marks_only_the_selected_organisation_for_onboarding(
    monkeypatch: pytest.MonkeyPatch,
):
    organisation_id = uuid4()
    organisation = _organisation(organisation_id, {"crm": {"provider": "copilotgtm"}})
    master_session = _MasterSession(organisation)
    _patch_tenant_session(monkeypatch, has_deal=True)

    service = AdminOrganisationService(master_session)  # type: ignore[arg-type]

    await service.update_organisation_app_settings(
        organisation_id=organisation_id,
        admin_id=uuid4(),
        settings=UpdateOrganisationAppSettingsRequest(
            onboarding={
                "enabled": True,
                "steps": [
                    {"id": "google", "skippable": False},
                    {"id": "whatsapp", "skippable": True},
                ],
            }
        ),
    )

    assert master_session.committed is True
    assert organisation.preferences["onboarding"] == {
        "enabled": True,
        "steps": [
            {"id": "google", "skippable": False},
            {"id": "whatsapp", "skippable": True},
        ],
    }
