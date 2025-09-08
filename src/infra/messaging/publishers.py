"""Centralized Redis publishing logic for messaging services.

Provides RedisPublisher with lazy connection and error management.
"""

from __future__ import annotations

import json
from src.infra.utils.app_state import fastapp
from src.infra.messaging.redis_client import get_redis_client


class RedisPublisher:
    def __init__(self, logger):
        self.logger = logger

    def get_client(self):
        redis_client = getattr(fastapp.state, "redis_client", None)
        if redis_client is None:
            try:
                redis_client = get_redis_client()
            except Exception as e:
                self.logger.warning("Redis client unavailable; skipping publish.")
                self.logger.debug("Redis init failure detail: %s", e)
                raise
        return redis_client

    def publish(self, channel: str, payload: dict, context_id: str = None):
        try:
            redis_client = self.get_client()
        except Exception:
            # error already logged in get_client
            return

        try:
            redis_client.publish(channel, json.dumps(payload))
            if context_id:
                self.logger.debug(
                    "Published update for %s on channel %s: %s",
                    context_id,
                    channel,
                    payload.get("status"),
                )
            else:
                self.logger.debug(
                    "Published update on channel %s: %s", channel, payload.get("status")
                )
        except Exception as exc:
            if context_id:
                self.logger.warning(
                    "Failed to publish for %s on %s: %s", context_id, channel, exc
                )
            else:
                self.logger.warning("Failed to publish on %s: %s", channel, exc)
