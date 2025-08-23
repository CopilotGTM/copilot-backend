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

from src.api.schemas.knowledge_upload_models import KnowledgeSourceEventUpdate
from src.infra.utils.app_state import fastapp
from src.infra.utils.logger import get_logger
from src.infra.messaging.redis_client import get_redis_client

logger = get_logger("knowledge_publish")


def _channel(organisation_id: UUID | str) -> str:
    return f"kb:{organisation_id}:sources"


def publish_source_status(
  organisation_id: UUID | str, 
  event: KnowledgeSourceEventUpdate
):  # pragma: no cover (thin wrapper)
  """Sync Redis publish for knowledge source status updates.

  Celery tasks run outside the FastAPI lifespan so the app-level redis client
  may not be initialised. We lazily create and cache a client when missing.
  """
  try:
    redis_client = getattr(fastapp.state, "redis_client", None)
    if redis_client is None:
      redis_client = get_redis_client()
      fastapp.state.redis_client = redis_client
  except Exception as e:  # pragma: no cover
    logger.warning("Redis client unavailable; skipping publish for %s", event.source_id)
    logger.debug("Redis init failure detail: %s", e)
    return

  payload = {
    "source_id": str(event.source_id),
    "status": event.status,
    "updated_at": event.updated_at,
    "error_message": event.error_message,
    "progress": event.progress,
    "thumbnail_small_url": event.thumbnail_small_url,
    "thumbnail_medium_url": event.thumbnail_medium_url,
  }
  try:
    redis_client.publish(_channel(organisation_id), json.dumps(payload))
    logger.debug(f"Published status update for {event.source_id}: {event.status}")
  except Exception as exc:
    logger.warning("Failed to publish source status for %s: %s", event.source_id, exc)