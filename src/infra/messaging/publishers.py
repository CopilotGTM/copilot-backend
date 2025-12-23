"""Centralized Redis publishing logic for messaging services.

Provides RedisPublisher with lazy connection and error management.
"""

from __future__ import annotations

import json

from src.infra.messaging.redis_client import get_redis_client


class RedisPublisher:
    def __init__(self, logger):
        self.logger = logger

    def get_client(self):
        """Get Redis client (works in both FastAPI and Celery contexts)."""
        try:
            # Use the centralized get_redis_client which handles pool initialization
            redis_client = get_redis_client()
            return redis_client
        except Exception as e:
            self.logger.warning("Redis client unavailable; skipping publish: %s", e)
            raise

    def publish(self, channel: str, payload: dict, context_id: str | None = None):
        try:
            redis_client = self.get_client()
        except Exception:
            # error already logged in get_client
            return

        try:
            redis_client.publish(channel, json.dumps(payload))
            # Use INFO level for org job publishes to aid debugging
            if context_id:
                self.logger.info(
                    "Published status update for %s on channel %s: status=%s, progress=%s",
                    context_id,
                    channel,
                    payload.get("status"),
                    payload.get("progress"),
                )
            else:
                self.logger.debug(
                    "Published update on channel %s: %s", channel, payload.get("status")
                )
        except Exception as exc:
            if context_id:
                self.logger.error("Failed to publish for %s on %s: %s", context_id, channel, exc)
            else:
                self.logger.warning("Failed to publish on %s: %s", channel, exc)
