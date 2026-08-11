"""Evaluate file-backed liveness and readiness for Kubernetes probes."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# These fixed, pod-local paths are the Kubernetes exec-probe contract, not shared temp inputs.
LIVE_PATH = Path("/tmp/salesforce-cdc-worker-live")  # noqa: S108  # nosec B108
READY_PATH = Path("/tmp/salesforce-cdc-worker-ready.json")  # noqa: S108  # nosec B108


def is_live(heartbeat: datetime, *, now: datetime | None = None) -> bool:
    """Require only a fresh event-loop heartbeat; provider health does not affect liveness."""
    current = now or datetime.now(UTC)
    return (current - _aware(heartbeat)).total_seconds() <= 60


def is_ready(state: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Require fresh discovery, no overflow, and every non-repairing desired connector."""
    discovered_at = _parse_datetime(state.get("discovered_at"))
    if discovered_at is None:
        return False
    current = now or datetime.now(UTC)
    return (
        (current - discovered_at).total_seconds() <= 120
        and int(state.get("capacity_exceeded") or 0) == 0
        and int(state.get("running") or 0) == int(state.get("desired") or 0)
    )


def main() -> int:
    """Return a process exit code suitable for Kubernetes exec probes."""
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--ready", action="store_true")
    args = parser.parse_args()
    try:
        if args.live:
            heartbeat = _parse_datetime(LIVE_PATH.read_text(encoding="utf-8").strip())
            return 0 if heartbeat is not None and is_live(heartbeat) else 1
        return 0 if is_ready(json.loads(READY_PATH.read_text(encoding="utf-8"))) else 1
    except (OSError, ValueError, json.JSONDecodeError):
        return 1


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


if __name__ == "__main__":
    raise SystemExit(main())
