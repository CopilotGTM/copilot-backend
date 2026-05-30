from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from src.api.routers.healthcheck import verify_internal_access
from starlette.requests import Request


def _build_request(client_host: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/internal/healthcheck",
        "headers": [],
        "client": (client_host, 12345),
        "scheme": "http",
        "server": ("testserver", 80),
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_verify_internal_access_rejects_internal_ip_without_configured_token(
    monkeypatch,
) -> None:
    monkeypatch.delenv("INTERNAL_HEALTH_TOKEN", raising=False)

    with pytest.raises(HTTPException) as exc_info:
        await verify_internal_access(_build_request("10.20.30.40"), credentials=None)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_verify_internal_access_rejects_internal_ip_with_wrong_bearer_token(
    monkeypatch,
) -> None:
    monkeypatch.setenv("INTERNAL_HEALTH_TOKEN", "secret-token")
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="wrong-token")

    with pytest.raises(HTTPException) as exc_info:
        await verify_internal_access(_build_request("10.20.30.40"), credentials=credentials)

    assert exc_info.value.status_code == 403


def test_integrations_scheduler_health_endpoint_returns_scheduler_status(
    client,
    mocker,
    monkeypatch,
) -> None:
    monkeypatch.setenv("INTERNAL_HEALTH_TOKEN", "secret-token")
    mocker.patch(
        "src.api.routers.healthcheck.get_scheduler_health",
        return_value={
            "healthy": False,
            "jobs": {
                "sync-deal-messages-all-orgs": {
                    "healthy": False,
                    "status": "stale",
                    "task_name": "integrations.sync_deal_messages_all_orgs",
                }
            },
        },
    )

    response = client.get(
        "/internal/health/integrations-scheduler",
        headers={"Authorization": "Bearer secret-token"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["jobs"]["sync-deal-messages-all-orgs"]["status"] == "stale"


def test_deal_scheduler_health_endpoint_returns_scheduler_status(
    client,
    mocker,
    monkeypatch,
) -> None:
    monkeypatch.setenv("INTERNAL_HEALTH_TOKEN", "secret-token")
    mocker.patch(
        "src.api.routers.healthcheck.get_deal_scheduler_health",
        return_value={
            "healthy": False,
            "jobs": {
                "deal-mapping": {
                    "healthy": False,
                    "status": "stale",
                    "task_name": "stakeholder.sync_all_deals_stakeholders_all_orgs",
                }
            },
        },
    )

    response = client.get(
        "/internal/health/deal-scheduler",
        headers={"Authorization": "Bearer secret-token"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["jobs"]["deal-mapping"]["status"] == "stale"


def test_knowledge_scheduler_health_endpoint_returns_scheduler_status(
    client,
    mocker,
    monkeypatch,
) -> None:
    monkeypatch.setenv("INTERNAL_HEALTH_TOKEN", "secret-token")
    mocker.patch(
        "src.api.routers.healthcheck.get_knowledge_scheduler_health",
        return_value={
            "healthy": False,
            "jobs": {
                "cleanup-stale-knowledge-jobs": {
                    "healthy": False,
                    "status": "stale",
                    "task_name": "knowledge.cleanup_stale_jobs_all_orgs",
                }
            },
        },
    )

    response = client.get(
        "/internal/health/knowledge-scheduler",
        headers={"Authorization": "Bearer secret-token"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["jobs"]["cleanup-stale-knowledge-jobs"]["status"] == "stale"


def test_detailed_healthcheck_preserves_unhealthy_component_payload(mocker):
    import src.api.routers.healthcheck as healthcheck_runtime

    # Reproduce a genuine unhealthy dependency so the handler must re-raise its own 503 payload.
    provider = mocker.Mock()
    provider.get_client_info.return_value = {"provider": "test"}

    mocker.patch.object(healthcheck_runtime.secrets_manager, "get_metrics", return_value={})
    mocker.patch.object(healthcheck_runtime.secrets_manager, "provider", provider, create=True)
    mocker.patch.object(
        healthcheck_runtime,
        "get_scheduler_health",
        return_value={"healthy": False, "component": "integrations"},
    )
    mocker.patch.object(
        healthcheck_runtime,
        "get_deal_scheduler_health",
        return_value={"healthy": True, "component": "deal"},
    )
    mocker.patch.object(
        healthcheck_runtime,
        "get_knowledge_scheduler_health",
        return_value={"healthy": True, "component": "knowledge"},
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(healthcheck_runtime.detailed_healthcheck(authorized=True))

    # HTTPException.detail is typed as str|None upstream but FastAPI allows Any.
    detail: Any = exc_info.value.detail
    assert exc_info.value.status_code == 503
    assert detail["status"] == "unhealthy"
    assert detail["integrations_scheduler"]["component"] == "integrations"
    assert detail["overall_healthy"] is False
