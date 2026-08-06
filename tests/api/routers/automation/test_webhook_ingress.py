"""Public automation webhook handler commit and bounded-body contracts."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import Response as FastAPIResponse
from src.api.routers.automation.triggers_handler import (
    receive_automation_webhook,
    rotate_agent_webhook_secret,
)
from src.api.schemas.automation_trigger_models import (
    AutomationWebhookCredentials,
    AutomationWebhookIngressResponse,
    AutomationWebhookRotateRequest,
)
from starlette.requests import Request


class Session:
    """Represent the Session test double used by the surrounding regression cases."""

    def __init__(self):
        self.committed = False
        self.rolled_back = False

    async def commit(self):
        """Expose the commit operation expected by the Session test double."""
        self.committed = True

    async def rollback(self):
        """Expose the rollback operation expected by the Session test double."""
        self.rolled_back = True


class Service:
    """Represent the Service test double used by the surrounding regression cases."""

    def __init__(self):
        self.session = Session()
        self.arguments = None

    async def ingest(self, **kwargs):
        """Expose the ingest operation expected by the Service test double."""
        self.arguments = kwargs
        return SimpleNamespace(envelope=SimpleNamespace(event_id=uuid4()))


def request(body: bytes, headers: dict[str, str]) -> Request:
    """Build the request fixture used by the surrounding regression cases."""
    sent = False

    async def receive():
        """Build the receive fixture used by the surrounding regression cases."""
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/webhooks/v1/automation/token",
            "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
            "client": ("127.0.0.1", 1234),
        },
        receive,
    )


@pytest.mark.asyncio
async def test_webhook_handler_passes_exact_raw_body_and_commits() -> None:
    """Verify the contract that webhook handler passes exact raw body and commits."""
    body = b'{ "risk": "high" }'
    service = Service()
    response = FastAPIResponse()

    result = await receive_automation_webhook(
        endpoint_token="token",
        request=request(
            body,
            {
                "content-length": str(len(body)),
                "x-copilotgtm-timestamp": "1785924000",
                "x-copilotgtm-signature": "sha256=signature",
                "x-copilotgtm-delivery-id": "provider-1",
            },
        ),
        response=response,
        service=service,  # type: ignore[arg-type]
        _rate_limit=None,
    )

    assert result.status == "SUCCESS"
    assert isinstance(result.data, AutomationWebhookIngressResponse)
    assert response.status_code == 202
    assert service.arguments == {
        "raw_body": body,
        "timestamp": "1785924000",
        "signature": "sha256=signature",
        "dedupe_key": "provider-1",
    }
    assert service.session.committed is True
    assert service.session.rolled_back is False


@pytest.mark.asyncio
async def test_webhook_rotation_handler_returns_the_secret_once_and_commits() -> None:
    """Verify the contract that webhook rotation handler returns the secret once and commits."""

    class OwnerService:
        """Represent the Owner Service test double used by the surrounding regression cases."""

        def __init__(self):
            self.session = Session()

        async def rotate_webhook_secret(self, agent_id, trigger_id, body):  # noqa: ANN001
            """Expose the rotate webhook secret operation expected by the Owner Service test double."""
            assert agent_id and trigger_id and body.expected_updated_at
            return AutomationWebhookCredentials(secret="whsec_rotated")

    service = OwnerService()
    body = AutomationWebhookRotateRequest(expected_updated_at="2026-08-05T10:00:00+00:00")

    result = await rotate_agent_webhook_secret(
        agent_id=uuid4(),
        trigger_id=uuid4(),
        body=body,
        response=FastAPIResponse(),
        service=service,  # type: ignore[arg-type]
        _rl_write=None,
    )

    assert result.data.secret == "whsec_rotated"
    assert result.data.endpoint_token is None
    assert service.session.committed is True
