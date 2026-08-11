from datetime import UTC, datetime, timedelta

from src.workers.salesforce_cdc_worker.healthcheck import is_live, is_ready


def test_liveness_only_requires_fresh_event_loop_heartbeat() -> None:
    now = datetime.now(UTC)
    assert is_live(now - timedelta(seconds=59), now=now) is True
    assert is_live(now - timedelta(seconds=61), now=now) is False


def test_readiness_requires_fresh_discovery_capacity_and_running_desired() -> None:
    now = datetime.now(UTC)
    assert (
        is_ready(
            {
                "discovered_at": now.isoformat(),
                "capacity_exceeded": 0,
                "desired": 7,
                "running": 7,
            },
            now=now,
        )
        is True
    )
    assert (
        is_ready(
            {
                "discovered_at": (now - timedelta(seconds=121)).isoformat(),
                "capacity_exceeded": 0,
                "desired": 7,
                "running": 7,
            },
            now=now,
        )
        is False
    )
