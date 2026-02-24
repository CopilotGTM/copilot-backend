"""Tests for CreateDealEditFormHandler."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from src.service.bots.form_handlers.create_deal_edit_handler import CreateDealEditFormHandler


@pytest.fixture
def handler() -> CreateDealEditFormHandler:
    return CreateDealEditFormHandler()


@pytest.fixture
def activity() -> dict:
    return {
        "from": {"id": "teams-user-123"},
        "channelData": {"tenant": {"id": "tenant-456"}},
    }


@pytest.fixture
def form_data() -> dict:
    return {
        "nonce": "valid-nonce-123",
        "organisation_id": "11111111-1111-1111-1111-111111111111",
        "name": "Acme Corp Deal",
        "amount": 50000,
        "currency": "USD",
        "description": "A big deal",
        "close_date": "2026-06-15",
        "priority": "high",
        "account_name": "Acme Corp",
        "account_domain": "acme.com",
        "pipeline_id": "22222222-2222-2222-2222-222222222222",
    }


@dataclass
class EditHandlerMocks:
    """Exposes mock objects from the identity_mocks fixture."""

    user: MagicMock
    org_session: AsyncMock


@pytest.fixture
def identity_mocks():
    """Mock identity resolution and DB sessions for happy-path edit handler tests."""
    mock_user = MagicMock()
    mock_user.organisation_id = UUID("11111111-1111-1111-1111-111111111111")

    mock_identity_service = MagicMock()
    mock_identity_service.get_user_for_external_identity = AsyncMock(return_value=(mock_user, None))

    mock_master_session = AsyncMock()
    mock_org_session = AsyncMock()

    # Default: no pipelines/stages
    empty_result = MagicMock()
    empty_result.all.return_value = []
    mock_org_session.execute = AsyncMock(return_value=empty_result)

    async def mock_acquire_master():
        yield mock_master_session

    async def mock_acquire_org(org_id):
        yield mock_org_session

    with (
        patch(
            "src.service.bots.identity_service.ExternalIdentityService",
            return_value=mock_identity_service,
        ),
        patch(
            "src.infra.database.sessions.acquire_master_async_session",
            mock_acquire_master,
        ),
        patch(
            "src.infra.database.sessions.acquire_org_async_session",
            mock_acquire_org,
        ),
    ):
        yield EditHandlerMocks(user=mock_user, org_session=mock_org_session)


# ---------------------------------------------------------------------------
# Nonce validation
# ---------------------------------------------------------------------------


class TestCreateDealEditHandlerNonce:
    """Nonce must be present and valid before the handler proceeds."""

    @pytest.mark.asyncio
    @patch("src.service.bots.form_handlers.create_deal_edit_handler.validate_and_consume_nonce")
    async def test_rejects_expired_nonce(
        self,
        mock_validate: MagicMock,
        handler: CreateDealEditFormHandler,
        activity: dict,
        form_data: dict,
    ) -> None:
        mock_validate.return_value = False

        result = await handler.handle(activity, form_data)

        assert result.success is False
        assert "expired" in result.message.lower()
        mock_validate.assert_called_once_with("deal_confirm", "valid-nonce-123")

    @pytest.mark.asyncio
    @patch("src.service.bots.form_handlers.create_deal_edit_handler.validate_and_consume_nonce")
    async def test_rejects_missing_nonce(
        self,
        mock_validate: MagicMock,
        handler: CreateDealEditFormHandler,
        activity: dict,
    ) -> None:
        result = await handler.handle(activity, {"name": "Acme Deal"})

        assert result.success is False
        assert "expired" in result.message.lower()
        mock_validate.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.service.bots.form_handlers.create_deal_edit_handler.validate_and_consume_nonce")
    async def test_rejects_empty_nonce(
        self,
        mock_validate: MagicMock,
        handler: CreateDealEditFormHandler,
        activity: dict,
    ) -> None:
        result = await handler.handle(activity, {"nonce": "", "name": "Deal"})

        assert result.success is False
        mock_validate.assert_not_called()


# ---------------------------------------------------------------------------
# Identity resolution failures
# ---------------------------------------------------------------------------


class TestCreateDealEditHandlerIdentity:
    """Handler must reject users that cannot be identified or have no org."""

    @pytest.mark.asyncio
    @patch(
        "src.service.bots.form_handlers.create_deal_edit_handler.validate_and_consume_nonce",
        return_value=True,
    )
    async def test_rejects_unknown_user(
        self,
        mock_validate: MagicMock,
        handler: CreateDealEditFormHandler,
        activity: dict,
        form_data: dict,
    ) -> None:
        mock_identity_service = MagicMock()
        mock_identity_service.get_user_for_external_identity = AsyncMock(return_value=(None, None))
        mock_master_session = AsyncMock()

        async def mock_acquire_master():
            yield mock_master_session

        with (
            patch(
                "src.service.bots.identity_service.ExternalIdentityService",
                return_value=mock_identity_service,
            ),
            patch(
                "src.infra.database.sessions.acquire_master_async_session",
                mock_acquire_master,
            ),
        ):
            result = await handler.handle(activity, form_data)

        assert result.success is False
        assert "could not identify" in result.message.lower()

    @pytest.mark.asyncio
    @patch(
        "src.service.bots.form_handlers.create_deal_edit_handler.validate_and_consume_nonce",
        return_value=True,
    )
    async def test_rejects_user_without_organisation(
        self,
        mock_validate: MagicMock,
        handler: CreateDealEditFormHandler,
        activity: dict,
        form_data: dict,
    ) -> None:
        mock_user = MagicMock()
        mock_user.organisation_id = None

        mock_identity_service = MagicMock()
        mock_identity_service.get_user_for_external_identity = AsyncMock(
            return_value=(mock_user, None)
        )
        mock_master_session = AsyncMock()

        async def mock_acquire_master():
            yield mock_master_session

        with (
            patch(
                "src.service.bots.identity_service.ExternalIdentityService",
                return_value=mock_identity_service,
            ),
            patch(
                "src.infra.database.sessions.acquire_master_async_session",
                mock_acquire_master,
            ),
        ):
            result = await handler.handle(activity, form_data)

        assert result.success is False
        assert "could not identify" in result.message.lower()


# ---------------------------------------------------------------------------
# Happy path — editable form generation
# ---------------------------------------------------------------------------


class TestCreateDealEditHandlerHappyPath:
    """Successful edit should return an Adaptive Card with editable form fields."""

    @pytest.mark.asyncio
    @patch(
        "src.service.bots.form_handlers.create_deal_edit_handler.create_nonce",
        return_value="new-edit-nonce",
    )
    @patch(
        "src.service.bots.form_handlers.create_deal_edit_handler.validate_and_consume_nonce",
        return_value=True,
    )
    async def test_returns_adaptive_card_payload(
        self,
        mock_validate: MagicMock,
        mock_create_nonce: MagicMock,
        handler: CreateDealEditFormHandler,
        activity: dict,
        form_data: dict,
        identity_mocks: EditHandlerMocks,
    ) -> None:
        result = await handler.handle(activity, form_data)

        assert result.success is True
        assert result.card_payload is not None

        card = result.card_payload
        assert card["type"] == "AdaptiveCard"
        assert "$schema" in card

        # Should have Input fields (editable), not FactSet (read-only preview)
        body_types = [item["type"] for item in card["body"]]
        assert any("Input" in t for t in body_types)

    @pytest.mark.asyncio
    @patch(
        "src.service.bots.form_handlers.create_deal_edit_handler.create_nonce",
        return_value="new-edit-nonce",
    )
    @patch(
        "src.service.bots.form_handlers.create_deal_edit_handler.validate_and_consume_nonce",
        return_value=True,
    )
    async def test_submit_and_cancel_actions(
        self,
        mock_validate: MagicMock,
        mock_create_nonce: MagicMock,
        handler: CreateDealEditFormHandler,
        activity: dict,
        form_data: dict,
        identity_mocks: EditHandlerMocks,
    ) -> None:
        result = await handler.handle(activity, form_data)
        card = result.card_payload

        submit_action = card["actions"][0]
        assert submit_action["verb"] == "create_deal_submit"
        assert submit_action["title"] == "Create Deal"

        cancel_action = card["actions"][1]
        assert cancel_action["verb"] == "create_deal_cancel"
        assert cancel_action["title"] == "Cancel"

    @pytest.mark.asyncio
    @patch(
        "src.service.bots.form_handlers.create_deal_edit_handler.create_nonce",
        return_value="new-edit-nonce",
    )
    @patch(
        "src.service.bots.form_handlers.create_deal_edit_handler.validate_and_consume_nonce",
        return_value=True,
    )
    async def test_new_nonce_in_submit_data(
        self,
        mock_validate: MagicMock,
        mock_create_nonce: MagicMock,
        handler: CreateDealEditFormHandler,
        activity: dict,
        form_data: dict,
        identity_mocks: EditHandlerMocks,
    ) -> None:
        """Editable form must carry a NEW nonce, not the consumed original."""
        result = await handler.handle(activity, form_data)
        card = result.card_payload

        submit_action = card["actions"][0]
        assert submit_action["data"]["nonce"] == "new-edit-nonce"
        assert submit_action["data"]["nonce"] != form_data["nonce"]

    @pytest.mark.asyncio
    @patch(
        "src.service.bots.form_handlers.create_deal_edit_handler.create_nonce",
        return_value="nonce",
    )
    @patch(
        "src.service.bots.form_handlers.create_deal_edit_handler.validate_and_consume_nonce",
        return_value=True,
    )
    async def test_prefilled_values_from_form_data(
        self,
        mock_validate: MagicMock,
        mock_create_nonce: MagicMock,
        handler: CreateDealEditFormHandler,
        activity: dict,
        form_data: dict,
        identity_mocks: EditHandlerMocks,
    ) -> None:
        """Editable form should be pre-filled with values from the original card."""
        result = await handler.handle(activity, form_data)
        card = result.card_payload
        body = card["body"]

        inputs = {item["id"]: item for item in body if item.get("type", "").startswith("Input.")}

        assert inputs["name"]["value"] == "Acme Corp Deal"
        assert inputs["amount"]["value"] == 50000
        assert inputs["currency"]["value"] == "USD"
        assert inputs["description"]["value"] == "A big deal"
        assert inputs["close_date"]["value"] == "2026-06-15"
        assert inputs["account_name"]["value"] == "Acme Corp"
        assert inputs["account_domain"]["value"] == "acme.com"

    @pytest.mark.asyncio
    @patch(
        "src.service.bots.form_handlers.create_deal_edit_handler.create_nonce",
        return_value="nonce",
    )
    @patch(
        "src.service.bots.form_handlers.create_deal_edit_handler.validate_and_consume_nonce",
        return_value=True,
    )
    async def test_pipeline_and_stage_dropdowns(
        self,
        mock_validate: MagicMock,
        mock_create_nonce: MagicMock,
        handler: CreateDealEditFormHandler,
        activity: dict,
        form_data: dict,
        identity_mocks: EditHandlerMocks,
    ) -> None:
        """When pipelines/stages exist in DB, dropdown choices should appear."""
        mock_pipeline = MagicMock()
        mock_pipeline.pipeline_id = UUID("22222222-2222-2222-2222-222222222222")
        mock_pipeline.name = "Sales Pipeline"
        mock_pipeline.is_default = True

        mock_stage = MagicMock()
        mock_stage.stage_id = UUID("33333333-3333-3333-3333-333333333333")
        mock_stage.pipeline_id = UUID("22222222-2222-2222-2222-222222222222")
        mock_stage.name = "Discovery"

        pipeline_result = MagicMock()
        pipeline_result.all.return_value = [mock_pipeline]
        stage_result = MagicMock()
        stage_result.all.return_value = [mock_stage]

        identity_mocks.org_session.execute = AsyncMock(side_effect=[pipeline_result, stage_result])

        result = await handler.handle(activity, form_data)
        card = result.card_payload
        body = card["body"]

        choice_sets = [item for item in body if item.get("type") == "Input.ChoiceSet"]
        choice_ids = {item["id"] for item in choice_sets}

        assert "pipeline_id" in choice_ids
        assert "stage" in choice_ids

        pipeline_input = next(c for c in choice_sets if c["id"] == "pipeline_id")
        assert pipeline_input["choices"][0]["title"] == "Sales Pipeline"

        stage_input = next(c for c in choice_sets if c["id"] == "stage")
        assert stage_input["choices"][0]["title"] == "Discovery"

    @pytest.mark.asyncio
    @patch(
        "src.service.bots.form_handlers.create_deal_edit_handler.create_nonce",
        return_value="nonce",
    )
    @patch(
        "src.service.bots.form_handlers.create_deal_edit_handler.validate_and_consume_nonce",
        return_value=True,
    )
    async def test_name_field_is_required(
        self,
        mock_validate: MagicMock,
        mock_create_nonce: MagicMock,
        handler: CreateDealEditFormHandler,
        activity: dict,
        form_data: dict,
        identity_mocks: EditHandlerMocks,
    ) -> None:
        """The name field should be marked as required in the form."""
        result = await handler.handle(activity, form_data)
        card = result.card_payload
        body = card["body"]

        name_input = next(item for item in body if item.get("id") == "name")
        assert name_input["isRequired"] is True
