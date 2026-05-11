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
    WhatsAppBotConnectResponse,
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
                ),
                AvailableBotIntegration(
                    provider="whatsapp",
                    name="WhatsApp Bot",
                    display_name="CopilotGTM WhatsApp Bot",
                    description="Chat with CopilotGTM from WhatsApp.",
                    logo_url="https://upload.wikimedia.org/wikipedia/commons/6/6b/WhatsApp.svg",
                    availability_status="available",
                    connection_mode="qr_deep_link",
                    installed=True,
                    identity_linked=False,
                    action_required="identity_link_required",
                ),
            ],
            total_available=2,
            total_installed=1,
            total_linked=0,
        )

    async def get_slack_connect_url(self) -> SlackBotConnectResponse:
        return SlackBotConnectResponse(
            provider="slack",
            connect_url="https://slack.com/oauth/v2/authorize?client_id=test",
            expires_at=None,
        )

    async def get_whatsapp_connect_url(self) -> WhatsAppBotConnectResponse:
        return WhatsAppBotConnectResponse(
            provider="whatsapp",
            connect_url="https://wa.me/15556300848?text=hi",
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
    assert body["data"]["total_available"] == 2
    assert body["data"]["bots"][0]["provider"] == "slack"
    assert body["data"]["bots"][0]["display_name"] == "CopilotGTM Slack Bot"
    assert body["data"]["bots"][1]["provider"] == "whatsapp"
    assert body["data"]["bots"][1]["display_name"] == "CopilotGTM WhatsApp Bot"


def test_slack_connect_route_returns_install_url():
    client = _client()

    resp = client.get("/users/integrations/bots/slack/connect")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["provider"] == "slack"
    assert body["data"]["connect_url"].startswith("https://slack.com/oauth/v2/authorize?")


def test_whatsapp_connect_route_returns_click_to_chat_url():
    client = _client()

    resp = client.get("/users/integrations/bots/whatsapp/connect")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["provider"] == "whatsapp"
    assert body["data"]["connect_url"] == "https://wa.me/15556300848?text=hi"
