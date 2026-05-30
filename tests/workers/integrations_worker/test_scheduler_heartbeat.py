from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from src.workers.integrations_worker.scheduler_heartbeat import get_scheduler_health


class _FakeRedis:
    def __init__(self, payloads: dict[str, dict[str, object]]) -> None:
        self._payloads = payloads

    def get(self, key: str) -> bytes | None:
        payload = self._payloads.get(key)
        if payload is None:
            return None
        return json.dumps(payload).encode("utf-8")


def test_get_scheduler_health_marks_recent_dispatch_as_healthy(mocker) -> None:
    now = datetime(2026, 4, 13, 12, 0, tzinfo=UTC)
    redis = _FakeRedis(
        {
            "integrations:scheduler:sync-deal-messages-all-orgs": {
                "status": "triggered",
                "job_name": "sync-deal-messages-all-orgs",
                "task_name": "integrations.sync_deal_messages_all_orgs",
                "triggered_at": (now - timedelta(minutes=10)).isoformat(),
            }
        }
    )
    mocker.patch(
        "src.workers.integrations_worker.scheduler_heartbeat.get_redis_client",
        return_value=redis,
    )

    health = get_scheduler_health(now=now)
    deal_messages = health["jobs"]["sync-deal-messages-all-orgs"]

    assert health["healthy"] is True
    assert deal_messages["healthy"] is True
    assert deal_messages["status"] == "healthy"


def test_get_scheduler_health_marks_missing_job_as_visible_but_non_blocking(mocker) -> None:
    mocker.patch(
        "src.workers.integrations_worker.scheduler_heartbeat.get_redis_client",
        return_value=_FakeRedis({}),
    )

    health = get_scheduler_health(now=datetime(2026, 4, 13, 12, 0, tzinfo=UTC))

    assert health["healthy"] is True
    assert health["jobs"]["sync-deal-messages-all-orgs"]["status"] == "missing"
    assert health["jobs"]["sync-deal-messages-all-orgs"]["healthy"] is True


def test_get_scheduler_health_marks_stale_dispatch_as_unhealthy(mocker) -> None:
    now = datetime(2026, 4, 13, 12, 0, tzinfo=UTC)
    redis = _FakeRedis(
        {
            "integrations:scheduler:sync-deal-messages-all-orgs": {
                "status": "triggered",
                "job_name": "sync-deal-messages-all-orgs",
                "task_name": "integrations.sync_deal_messages_all_orgs",
                "triggered_at": (now - timedelta(hours=2)).isoformat(),
            }
        }
    )
    mocker.patch(
        "src.workers.integrations_worker.scheduler_heartbeat.get_redis_client",
        return_value=redis,
    )

    health = get_scheduler_health(now=now)

    assert health["jobs"]["sync-deal-messages-all-orgs"]["status"] == "stale"
    assert health["jobs"]["sync-deal-messages-all-orgs"]["healthy"] is False


def test_get_scheduler_health_marks_failed_dispatch_as_unhealthy(mocker) -> None:
    now = datetime(2026, 4, 13, 12, 0, tzinfo=UTC)
    redis = _FakeRedis(
        {
            "integrations:scheduler:sync-deal-messages-all-orgs": {
                "status": "failed",
                "job_name": "sync-deal-messages-all-orgs",
                "task_name": "integrations.sync_deal_messages_all_orgs",
                "failed_at": (now - timedelta(minutes=5)).isoformat(),
                "error": "boom",
            }
        }
    )
    mocker.patch(
        "src.workers.integrations_worker.scheduler_heartbeat.get_redis_client",
        return_value=redis,
    )

    health = get_scheduler_health(now=now)

    assert health["jobs"]["sync-deal-messages-all-orgs"]["status"] == "failed"
    assert health["jobs"]["sync-deal-messages-all-orgs"]["healthy"] is False
