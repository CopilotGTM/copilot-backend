from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.deal_channel_messages_service import (
    DealChannelMessagesService,
    DealLinkedChannelInfo,
)
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo


@pytest.mark.asyncio
async def test_teams_channel_messages_are_skipped_without_channel_message_scope(monkeypatch):
    service = DealChannelMessagesService(None, uuid4())
    channel_info = DealLinkedChannelInfo(
        external_channel_id="19:channel",
        channel_id=uuid4(),
        integration_id=uuid4(),
        channel_metadata={"provider": "teams", "team_id": "team-1"},
        channel_name="Sales",
        channel_type="public",
        last_synced_at=None,
        available_integrations=1,
    )
    token_info = IntegrationTokenInfo(
        access_token="token",
        integration_type=IntegrationType.TEAMS,
        organisation_id=uuid4(),
        scopes=[
            "offline_access",
            "User.Read",
            "Team.ReadBasic.All",
            "Channel.ReadBasic.All",
            "Chat.Read",
        ],
    )

    async def fail_if_called(**kwargs):  # noqa: ANN003
        raise AssertionError("channel message fetch should be skipped without scope")

    monkeypatch.setattr(
        "src.service.integrations.deal_channel_messages_service.fetch_messages",
        fail_if_called,
    )

    result = await service._fetch_messages_with_connection_id(
        channel_info=channel_info,
        connection_id="conn-1",
        token_info=token_info,
    )

    assert result.messages == []
    assert result.delta_link is None
