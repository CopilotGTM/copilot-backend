from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from src.workers.knowledge_worker.scheduler_heartbeat import get_scheduler_health


class _FakeRedis:
    def __init__(self, payloads: dict[str, dict[str, object]]) -> None:
        self._payloads = payloads

    def get(self, key: str) -> bytes | None:
        payload = self._payloads.get(key)
        if payload is None:
            return None
        return json.dumps(payload).encode("utf-8")


def test_get_scheduler_health_marks_recent_knowledge_cleanup_dispatch_as_healthy(mocker) -> None:
    now = datetime(2026, 4, 13, 12, 0, tzinfo=UTC)
    redis = _FakeRedis(
        {
            "knowledge:scheduler:cleanup-stale-knowledge-jobs": {
                "status": "triggered",
                "job_name": "cleanup-stale-knowledge-jobs",
                "task_name": "knowledge.cleanup_stale_jobs_all_orgs",
                "triggered_at": (now - timedelta(hours=5)).isoformat(),
            }
        }
    )
    mocker.patch(
        "src.workers.knowledge_worker.scheduler_heartbeat.get_redis_client",
        return_value=redis,
    )

    health = get_scheduler_health(now=datetime(2026, 4, 13, 12, 0, tzinfo=UTC))
    cleanup = health["jobs"]["cleanup-stale-knowledge-jobs"]

    assert health["healthy"] is True
    assert cleanup["healthy"] is True
    assert cleanup["status"] == "healthy"
    assert cleanup["schedule"] == "0 */6 * * *"
