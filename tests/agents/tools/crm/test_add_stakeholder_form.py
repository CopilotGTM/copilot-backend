"""Tests for add_stakeholder interactive form (card sending)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from src.agents.tools.crm.add_stakeholder import add_stakeholder_form


@pytest.fixture
def mock_context() -> MagicMock:
    ctx = MagicMock()
    ctx.organisation_id = UUID("11111111-1111-1111-1111-111111111111")
    ctx.channel = "teams"
    ctx.metadata = {
        "service_url": "https://smba.trafficmanager.net/teams/",
        "conversation_id": "conv-123",
    }
    return ctx


DEAL_ID = "22222222-2222-2222-2222-222222222222"
DEAL_NAME = "Acme Corp"


class TestAddStakeholderForm:
    """Tests for the add_stakeholder_form core function."""

    @pytest.mark.asyncio
    async def test_missing_org_returns_none(self, mock_context: MagicMock) -> None:
        mock_context.organisation_id = None

        result = await add_stakeholder_form(
            context=mock_context, deal_id=DEAL_ID, deal_name=DEAL_NAME
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_missing_service_url_returns_error(self, mock_context: MagicMock) -> None:
        mock_context.metadata = {"conversation_id": "conv-123"}

        result = await add_stakeholder_form(
            context=mock_context, deal_id=DEAL_ID, deal_name=DEAL_NAME
        )

        assert result is not None
        assert result["status"] == "error"
        assert "channel context" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_missing_conversation_id_returns_error(self, mock_context: MagicMock) -> None:
        mock_context.metadata = {"service_url": "https://smba.trafficmanager.net/teams/"}

        result = await add_stakeholder_form(
            context=mock_context, deal_id=DEAL_ID, deal_name=DEAL_NAME
        )

        assert result is not None
        assert result["status"] == "error"

    @pytest.mark.asyncio
    @patch("src.service.bots.teams_bot_service.TeamsBotService")
    @patch("src.agents.tools.crm.add_stakeholder.get_card_builder")
    async def test_successful_form_send(
        self,
        mock_builder_fn: MagicMock,
        mock_bot_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_builder = MagicMock()
        mock_builder.build = MagicMock(return_value={"text": "card"})
        mock_builder_fn.return_value = mock_builder

        mock_bot = MagicMock()
        mock_bot.send_proactive_payload = AsyncMock(return_value=True)
        mock_bot_cls.return_value = mock_bot

        result = await add_stakeholder_form(
            context=mock_context, deal_id=DEAL_ID, deal_name=DEAL_NAME
        )

        assert result is not None
        assert result["status"] == "form_sent"
        assert DEAL_NAME in result["message"]
        mock_bot.send_proactive_payload.assert_called_once()

    @pytest.mark.asyncio
    @patch("src.service.bots.teams_bot_service.TeamsBotService")
    @patch("src.agents.tools.crm.add_stakeholder.get_card_builder")
    async def test_prefilled_fields_passed_to_builder(
        self,
        mock_builder_fn: MagicMock,
        mock_bot_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        """Pre-filled fields should be included in the form definition."""
        mock_builder = MagicMock()
        mock_builder.build = MagicMock(return_value={"text": "card"})
        mock_builder_fn.return_value = mock_builder

        mock_bot = MagicMock()
        mock_bot.send_proactive_payload = AsyncMock(return_value=True)
        mock_bot_cls.return_value = mock_bot

        result = await add_stakeholder_form(
            context=mock_context,
            deal_id=DEAL_ID,
            deal_name=DEAL_NAME,
            email="john@acme.com",
            name="John Smith",
            job_title="VP of Sales",
            phone_number="+1 555 123 4567",
            stance="CHAMPION",
            decision_power="DECISION_MAKER",
        )

        assert result is not None
        assert result["status"] == "form_sent"

        # Verify the builder was called with a FormDefinition containing our fields
        form_def = mock_builder.build.call_args[0][0]
        field_values = {f.id: f.value for f in form_def.fields}
        assert field_values["email"] == "john@acme.com"
        assert field_values["name"] == "John Smith"
        assert field_values["job_title"] == "VP of Sales"
        assert field_values["phone_number"] == "+1 555 123 4567"
        assert field_values["stance"] == "CHAMPION"
        assert field_values["decision_power"] == "DECISION_MAKER"

    @pytest.mark.asyncio
    @patch("src.service.bots.teams_bot_service.TeamsBotService")
    @patch("src.agents.tools.crm.add_stakeholder.get_card_builder")
    async def test_none_fields_still_build_form(
        self,
        mock_builder_fn: MagicMock,
        mock_bot_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        """All-None optional fields should still produce a valid form."""
        mock_builder = MagicMock()
        mock_builder.build = MagicMock(return_value={"text": "card"})
        mock_builder_fn.return_value = mock_builder

        mock_bot = MagicMock()
        mock_bot.send_proactive_payload = AsyncMock(return_value=True)
        mock_bot_cls.return_value = mock_bot

        result = await add_stakeholder_form(
            context=mock_context, deal_id=DEAL_ID, deal_name=DEAL_NAME
        )

        assert result is not None
        assert result["status"] == "form_sent"

        form_def = mock_builder.build.call_args[0][0]
        assert len(form_def.fields) == 6  # email, name, job_title, phone, stance, decision_power

    @pytest.mark.asyncio
    @patch("src.service.bots.teams_bot_service.TeamsBotService")
    @patch("src.agents.tools.crm.add_stakeholder.get_card_builder")
    async def test_form_metadata_contains_org_and_deal(
        self,
        mock_builder_fn: MagicMock,
        mock_bot_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_builder = MagicMock()
        mock_builder.build = MagicMock(return_value={"text": "card"})
        mock_builder_fn.return_value = mock_builder

        mock_bot = MagicMock()
        mock_bot.send_proactive_payload = AsyncMock(return_value=True)
        mock_bot_cls.return_value = mock_bot

        await add_stakeholder_form(context=mock_context, deal_id=DEAL_ID, deal_name=DEAL_NAME)

        form_def = mock_builder.build.call_args[0][0]
        assert form_def.metadata["organisation_id"] == str(mock_context.organisation_id)
        assert form_def.metadata["deal_id"] == DEAL_ID
        assert form_def.metadata["deal_name"] == DEAL_NAME

    @pytest.mark.asyncio
    @patch("src.service.bots.teams_bot_service.TeamsBotService")
    @patch("src.agents.tools.crm.add_stakeholder.get_card_builder")
    async def test_send_failure_returns_error(
        self,
        mock_builder_fn: MagicMock,
        mock_bot_cls: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_builder = MagicMock()
        mock_builder.build = MagicMock(return_value={"text": "card"})
        mock_builder_fn.return_value = mock_builder

        mock_bot = MagicMock()
        mock_bot.send_proactive_payload = AsyncMock(return_value=False)
        mock_bot_cls.return_value = mock_bot

        result = await add_stakeholder_form(
            context=mock_context, deal_id=DEAL_ID, deal_name=DEAL_NAME
        )

        assert result is not None
        assert result["status"] == "error"
        assert "failed" in result["message"].lower()

    @pytest.mark.asyncio
    @patch("src.agents.tools.crm.add_stakeholder.get_card_builder")
    async def test_unsupported_channel_returns_error(
        self,
        mock_builder_fn: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        mock_builder_fn.side_effect = ValueError("No card builder for channel: slack")

        result = await add_stakeholder_form(
            context=mock_context, deal_id=DEAL_ID, deal_name=DEAL_NAME
        )

        assert result is not None
        assert result["status"] == "error"
        assert "not supported" in result["message"].lower()
