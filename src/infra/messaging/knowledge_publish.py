"""Helpers to publish knowledge source status updates to Redis.

Channel naming:
  kb:{organisation_id}:sources  -> fan-out for all source status changes in an org.

Payload (JSON):
  {
    "source_id": str,
    "status": str,
    "updated_at": iso8601 or null,
    "error_message": str | null
  }

Lightweight (only core fields). API layer can enrich on snapshot if needed.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID
import json

from src.infra.utils.app_state import fastapp
from src.infra.utils.logger import get_logger

logger = get_logger("knowledge_publish")


def _channel(organisation_id: UUID | str) -> str:
    return f"kb:{organisation_id}:sources"


def publish_source_status(organisation_id: UUID | str, source_id: UUID | str, status: str, updated_at: Any = None, error_message: str | None = None):  # pragma: no cover (thin wrapper)
    """Sync Redis publish for knowledge source status updates.
    
    Note: This is the sync version used in Celery tasks. For async contexts,
    use publish_source_status_async from redis_pubsub module.
    """
    try:
        redis_client = fastapp.state.redis_client  # sync redis client
    except Exception:
        logger.warning("Redis client unavailable; skipping publish for %s", source_id)
        return
    payload = {
        "source_id": str(source_id),
        "status": status,
        "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at,
        "error_message": error_message,
    }
    try:
        redis_client.publish(_channel(organisation_id), json.dumps(payload))
        logger.debug(f"Published status update for {source_id}: {status}")
    except Exception as exc:
        logger.warning("Failed to publish source status for %s: %s", source_id, exc)