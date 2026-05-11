from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.api.routers.integrations.bot_integration_routes import (
    get_bot_integration_service,
    router,
)
from src.api.schemas.integrations.bot_integration_schemas import (
    AvailableBotIntegration,
    AvailableBotIntegrationsResponse,
    SlackBotConnectResponse,
)


class _FakeBotIntegrationService:
    async def list_available_bots(self) -> AvailableBotIntegrationsResponse:
        return AvailableBotIntegrationsResponse(
            bots=[
                AvailableBotIntegration(
                    provider="slack",
                    name="Slack Bot",
                    display_name="CopilotGTM Slack Bot",
                    description="Chat with CopilotGTM from Slack.",
                    logo_url="/integrations/slack.png",
                    availability_status="available",
                    installed=False,
                    identity_linked=False,
                    action_required="install_required",
                )
            ],
            total_available=1,
            total_installed=0,
            total_linked=0,
        )

    async def get_slack_connect_url(self) -> SlackBotConnectResponse:
        return SlackBotConnectResponse(
            provider="slack",
            connect_url="https://slack.com/oauth/v2/authorize?client_id=test",
            expires_at=None,
        )


def _override_bot_service():
    return _FakeBotIntegrationService()


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/users/integrations")
    app.dependency_overrides[get_bot_integration_service] = _override_bot_service
    return TestClient(app)


def test_available_bots_route_returns_slack_card():
    client = _client()

    resp = client.get("/users/integrations/bots/available")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["total_available"] == 1
    assert body["data"]["bots"][0]["provider"] == "slack"
    assert body["data"]["bots"][0]["display_name"] == "CopilotGTM Slack Bot"


def test_slack_connect_route_returns_install_url():
    client = _client()

    resp = client.get("/users/integrations/bots/slack/connect")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["provider"] == "slack"
    assert body["data"]["connect_url"].startswith("https://slack.com/oauth/v2/authorize?")
