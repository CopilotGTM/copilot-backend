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
