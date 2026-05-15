from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.service.orgnisation.organisation_policy_service import (
    OrganisationPolicyDeniedError,
    OrganisationPolicyService,
)


class _Session:
    def __init__(self, preferences):
        self._organisation = SimpleNamespace(preferences=preferences)

    async def get(self, _model, _organisation_id):
        return self._organisation


@pytest.mark.asyncio
async def test_policy_service_allows_internal_crm_scope_by_default():
    service = OrganisationPolicyService(_Session({"crm": {"provider": "copilotgtm"}}))

    assert await service.has_crm_write_scope(uuid4(), "deals:create") is True


@pytest.mark.asyncio
async def test_policy_service_rejects_scope_missing_from_policy():
    service = OrganisationPolicyService(
        _Session(
            {
                "crm": {
                    "provider": "copilotgtm",
                    "write_policy": {"allowed_scopes": ["leads:create"]},
                }
            }
        )
    )

    with pytest.raises(OrganisationPolicyDeniedError) as exc_info:
        await service.require_crm_write_scope(uuid4(), "deals:create")

    assert exc_info.value.code == "crm_write_scope_disabled"


@pytest.mark.asyncio
async def test_policy_service_rejects_disabled_channel():
    service = OrganisationPolicyService(_Session({"channels": {"enabled": ["teams"]}}))

    with pytest.raises(OrganisationPolicyDeniedError) as exc_info:
        await service.require_channel_enabled(uuid4(), "slack")

    assert exc_info.value.code == "channel_disabled"
