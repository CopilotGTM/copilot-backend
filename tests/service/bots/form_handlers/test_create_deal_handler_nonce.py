"""Tests for nonce validation in CreateDealFormHandler."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from src.service.bots.form_handlers.create_deal_handler import CreateDealFormHandler
from src.service.orgnisation.organisation_policy_service import OrganisationPolicyDeniedError


@pytest.fixture
def handler() -> CreateDealFormHandler:
    return CreateDealFormHandler()


@pytest.fixture
def activity() -> dict:
    return {
        "from": {"id": "teams-user-123"},
        "channelData": {"tenant": {"id": "tenant-456"}},
    }


class TestCreateDealHandlerNonce:
    """Tests for nonce validation."""

    @pytest.mark.asyncio
    @patch("src.service.bots.form_handlers.create_deal_handler.validate_and_consume_nonce")
    async def test_rejects_expired_nonce(
        self,
        mock_validate: MagicMock,
        handler: CreateDealFormHandler,
        activity: dict,
    ) -> None:
        mock_validate.return_value = False

        result = await handler.handle(
            activity,
            {
                "nonce": "expired-nonce",
                "name": "Acme Deal",
                "organisation_id": "11111111-1111-1111-1111-111111111111",
            },
        )

        assert result.success is False
        assert "expired" in result.message.lower()
        mock_validate.assert_called_once_with("deal_confirm", "expired-nonce")

    @pytest.mark.asyncio
    @patch("src.service.bots.form_handlers.create_deal_handler.validate_and_consume_nonce")
    async def test_rejects_missing_nonce(
        self,
        mock_validate: MagicMock,
        handler: CreateDealFormHandler,
        activity: dict,
    ) -> None:
        result = await handler.handle(
            activity,
            {
                "name": "Acme Deal",
                "organisation_id": "11111111-1111-1111-1111-111111111111",
            },
        )

        assert result.success is False
        assert "expired" in result.message.lower() or "invalid" in result.message.lower()

    @pytest.mark.asyncio
    @patch("src.service.bots.form_handlers.create_deal_handler.validate_and_consume_nonce")
    async def test_rejects_when_org_policy_disables_create_scope(
        self,
        mock_validate: MagicMock,
        handler: CreateDealFormHandler,
        activity: dict,
    ) -> None:
        mock_validate.return_value = True
        user = SimpleNamespace(
            organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
            user_id=UUID("22222222-2222-2222-2222-222222222222"),
        )

        with (
            patch(
                "src.service.bots.form_handlers.create_deal_handler.resolve_user_from_activity",
                new=AsyncMock(return_value=(user, None)),
            ),
            patch(
                "src.service.bots.form_handlers.create_deal_handler.ensure_crm_write_scope",
                new=AsyncMock(
                    side_effect=OrganisationPolicyDeniedError(
                        "Internal CRM write scope 'deals:create' is disabled for this organisation",
                        code="crm_write_scope_disabled",
                    )
                ),
            ),
        ):
            result = await handler.handle(
                activity,
                {
                    "nonce": "valid-nonce",
                    "name": "Acme Deal",
                    "organisation_id": "11111111-1111-1111-1111-111111111111",
                },
            )

        assert result.success is False
        assert "deals:create" in result.message
