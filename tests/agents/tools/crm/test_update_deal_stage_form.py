"""Tests for update_deal_stage confirmation form (stage selection)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from src.agents.tools.crm.update_deal_stage import update_deal_stage_form


@pytest.fixture
def mock_context() -> MagicMock:
    ctx = MagicMock()
    ctx.organisation_id = UUID("11111111-1111-1111-1111-111111111111")
    ctx.channel = "teams"
    ctx.metadata = {
        "service_url": "https://smba.trafficmanager.net/teams/",
        "conversation_id": "conv-123",
    }

    # Mock acquire_session as async context manager
    mock_session = AsyncMock()
    async_cm = AsyncMock()
    async_cm.__aenter__ = AsyncMock(return_value=mock_session)
    async_cm.__aexit__ = AsyncMock(return_value=False)
    ctx.acquire_session = MagicMock(return_value=async_cm)

    return ctx


DEAL_ID = "22222222-2222-2222-2222-222222222222"
DEAL_NAME = "Acme Corp"


class TestUpdateDealStageForm:
    """Tests for the update_deal_stage_form core function."""

    @pytest.mark.asyncio
    async def test_missing_org_returns_none(self, mock_context: MagicMock) -> None:
        mock_context.organisation_id = None

        result = await update_deal_stage_form(
            context=mock_context, deal_id=DEAL_ID, deal_name=DEAL_NAME
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_missing_service_url_returns_error(self, mock_context: MagicMock) -> None:
        mock_context.metadata = {"conversation_id": "conv-123"}

        result = await update_deal_stage_form(
            context=mock_context, deal_id=DEAL_ID, deal_name=DEAL_NAME
        )

        assert result is not None
        assert result["status"] == "error"
        assert "channel context" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_missing_conversation_id_returns_error(self, mock_context: MagicMock) -> None:
        mock_context.metadata = {"service_url": "https://smba.trafficmanager.net/teams/"}

        result = await update_deal_stage_form(
            context=mock_context, deal_id=DEAL_ID, deal_name=DEAL_NAME
        )

        assert result is not None
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_invalid_deal_id_returns_error(self, mock_context: MagicMock) -> None:
        result = await update_deal_stage_form(
            context=mock_context, deal_id="not-a-uuid", deal_name=DEAL_NAME
        )

        assert result is not None
        assert result["status"] == "error"
        assert "invalid" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_deal_not_found_returns_error(self, mock_context: MagicMock) -> None:
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=None)))

        result = await update_deal_stage_form(
            context=mock_context, deal_id=DEAL_ID, deal_name=DEAL_NAME
        )

        assert result is not None
        assert result["status"] == "error"
        assert "not found" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_external_crm_deal_returns_error(self, mock_context: MagicMock) -> None:
        """External CRM deals are read-only and cannot have stage updates."""
        deal_row = MagicMock()
        deal_row.pipeline_id = UUID("33333333-3333-3333-3333-333333333333")
        deal_row.stage_id = UUID("44444444-4444-4444-4444-444444444444")
        deal_row.integration_id = UUID("55555555-5555-5555-5555-555555555555")

        # Integration type is NOT INTERNAL_CRM
        int_row = MagicMock()
        int_row.integration_type = "HUBSPOT"

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(first=MagicMock(return_value=deal_row)),
                MagicMock(first=MagicMock(return_value=int_row)),
            ]
        )

        result = await update_deal_stage_form(
            context=mock_context, deal_id=DEAL_ID, deal_name=DEAL_NAME
        )

        assert result is not None
        assert result["status"] == "error"
        assert "read-only" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_no_pipeline_returns_error(self, mock_context: MagicMock) -> None:
        deal_row = MagicMock()
        deal_row.pipeline_id = None
        deal_row.stage_id = None
        deal_row.integration_id = UUID("55555555-5555-5555-5555-555555555555")

        int_row = MagicMock()
        int_row.integration_type = "internal_crm"

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(first=MagicMock(return_value=deal_row)),
                MagicMock(first=MagicMock(return_value=int_row)),
            ]
        )

        result = await update_deal_stage_form(
            context=mock_context, deal_id=DEAL_ID, deal_name=DEAL_NAME
        )

        assert result is not None
        assert result["status"] == "error"
        assert "pipeline" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_no_stages_returns_error(self, mock_context: MagicMock) -> None:
        deal_row = MagicMock()
        deal_row.pipeline_id = UUID("33333333-3333-3333-3333-333333333333")
        deal_row.stage_id = UUID("44444444-4444-4444-4444-444444444444")
        deal_row.integration_id = UUID("55555555-5555-5555-5555-555555555555")

        int_row = MagicMock()
        int_row.integration_type = "internal_crm"

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(first=MagicMock(return_value=deal_row)),
                MagicMock(first=MagicMock(return_value=int_row)),
                MagicMock(all=MagicMock(return_value=[])),  # No stages
            ]
        )

        result = await update_deal_stage_form(
            context=mock_context, deal_id=DEAL_ID, deal_name=DEAL_NAME
        )

        assert result is not None
        assert result["status"] == "error"
        assert "no stages" in result["message"].lower()

    @pytest.mark.asyncio
    @patch("src.service.bots.teams_bot_service.TeamsBotService")
    @patch("src.agents.tools.crm.update_deal_stage.get_card_builder")
    async def test_successful_form_send(
        self,
        mock_builder_fn: MagicMock,
        mock_bot_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        deal_row = MagicMock()
        deal_row.pipeline_id = UUID("33333333-3333-3333-3333-333333333333")
        deal_row.stage_id = UUID("44444444-4444-4444-4444-444444444444")
        deal_row.integration_id = UUID("55555555-5555-5555-5555-555555555555")

        int_row = MagicMock()
        int_row.integration_type = "internal_crm"

        stage_row = MagicMock()
        stage_row.stage_id = UUID("44444444-4444-4444-4444-444444444444")
        stage_row.name = "Negotiation"

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(first=MagicMock(return_value=deal_row)),
                MagicMock(first=MagicMock(return_value=int_row)),
                MagicMock(all=MagicMock(return_value=[stage_row])),
            ]
        )

        mock_builder = MagicMock()
        mock_builder.build = MagicMock(return_value={"text": "card"})
        mock_builder_fn.return_value = mock_builder

        mock_bot = MagicMock()
        mock_bot.send_proactive_payload = AsyncMock(return_value=True)
        mock_bot_cls.return_value = mock_bot

        result = await update_deal_stage_form(
            context=mock_context, deal_id=DEAL_ID, deal_name=DEAL_NAME
        )

        assert result is not None
        assert result["status"] == "form_sent"
        mock_bot.send_proactive_payload.assert_called_once()

    @pytest.mark.asyncio
    @patch("src.service.bots.teams_bot_service.TeamsBotService")
    @patch("src.agents.tools.crm.update_deal_stage.get_card_builder")
    async def test_send_failure_returns_error(
        self,
        mock_builder_fn: MagicMock,
        mock_bot_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        deal_row = MagicMock()
        deal_row.pipeline_id = UUID("33333333-3333-3333-3333-333333333333")
        deal_row.stage_id = UUID("44444444-4444-4444-4444-444444444444")
        deal_row.integration_id = UUID("55555555-5555-5555-5555-555555555555")

        int_row = MagicMock()
        int_row.integration_type = "internal_crm"

        stage_row = MagicMock()
        stage_row.stage_id = UUID("44444444-4444-4444-4444-444444444444")
        stage_row.name = "Negotiation"

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(first=MagicMock(return_value=deal_row)),
                MagicMock(first=MagicMock(return_value=int_row)),
                MagicMock(all=MagicMock(return_value=[stage_row])),
            ]
        )

        mock_builder = MagicMock()
        mock_builder.build = MagicMock(return_value={"text": "card"})
        mock_builder_fn.return_value = mock_builder

        mock_bot = MagicMock()
        mock_bot.send_proactive_payload = AsyncMock(return_value=False)
        mock_bot_cls.return_value = mock_bot

        result = await update_deal_stage_form(
            context=mock_context, deal_id=DEAL_ID, deal_name=DEAL_NAME
        )

        assert result is not None
        assert result["status"] == "error"
        assert "failed" in result["message"].lower()

    @pytest.mark.asyncio
    @patch("src.agents.tools.crm.update_deal_stage.get_card_builder")
    async def test_unsupported_channel_returns_error(
        self,
        mock_builder_fn: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        """get_card_builder raises ValueError for unsupported channels."""
        deal_row = MagicMock()
        deal_row.pipeline_id = UUID("33333333-3333-3333-3333-333333333333")
        deal_row.stage_id = UUID("44444444-4444-4444-4444-444444444444")
        deal_row.integration_id = UUID("55555555-5555-5555-5555-555555555555")

        int_row = MagicMock()
        int_row.integration_type = "internal_crm"

        stage_row = MagicMock()
        stage_row.stage_id = UUID("44444444-4444-4444-4444-444444444444")
        stage_row.name = "Negotiation"

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(first=MagicMock(return_value=deal_row)),
                MagicMock(first=MagicMock(return_value=int_row)),
                MagicMock(all=MagicMock(return_value=[stage_row])),
            ]
        )

        mock_builder_fn.side_effect = ValueError("No card builder for channel: slack")

        result = await update_deal_stage_form(
            context=mock_context, deal_id=DEAL_ID, deal_name=DEAL_NAME
        )

        assert result is not None
        assert result["status"] == "error"
        assert "not supported" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_db_error_returns_error(self, mock_context: MagicMock) -> None:
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(side_effect=RuntimeError("DB connection lost"))

        result = await update_deal_stage_form(
            context=mock_context, deal_id=DEAL_ID, deal_name=DEAL_NAME
        )

        assert result is not None
        assert result["status"] == "error"
