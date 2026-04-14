from __future__ import annotations


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
