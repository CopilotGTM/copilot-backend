from __future__ import annotations

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
                "sync-calendar-events": {
                    "healthy": False,
                    "status": "stale",
                    "task_name": "integrations.sync_calendar_all_orgs",
                }
            },
        },
    )

    response = client.get(
        "/internal/health/integrations-scheduler",
        headers={"Authorization": "Bearer secret-token"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["jobs"]["sync-calendar-events"]["status"] == "stale"


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
