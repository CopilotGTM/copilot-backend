"""Redis pub/sub subscriber for forwarding messages to WebSocket clients.

This module subscribes to a Redis channel and forwards messages to connected
WebSocket clients using Socket.IO. Uses a persistent thread-local event loop
via run_async() to prevent memory leaks.
"""

import json
import threading
import time

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from src.api.events.socket_app import sio
from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger

logger = get_logger()
WEBSOCKET_NAMESPACE = "/websocket/v1"
REDIS_CHANNEL = "websocket_messages"


async def emit_websocket_message(event: str, message_data: dict, sid: str) -> None:
    """Emit message to websocket client asynchronously."""
    try:
        await sio.emit(event, message_data, room=sid, namespace=WEBSOCKET_NAMESPACE)
        logger.info("Successfully emitted message to websocket client %s", sid)
    except OSError as e:
        logger.error("Error emitting websocket message: %s", e, exc_info=True)


def _process_redis_message(message: dict) -> None:
    """Process a single Redis pub/sub message."""
    data = json.loads(message["data"])
    sid = data["sid"]
    event = data["event"]
    message_data = data["data"]

    # Use persistent thread-local event loop to prevent memory leaks
    run_async(emit_websocket_message(event, message_data, sid))


def _subscribe_and_listen(pubsub) -> None:
    """Subscribe to Redis channel and process incoming messages."""
    logger.info("Subscribing to Redis channel %s", REDIS_CHANNEL)
    pubsub.subscribe(REDIS_CHANNEL)
    logger.info("Successfully subscribed to Redis channel %s", REDIS_CHANNEL)

    while True:
        try:
            logger.debug("Waiting for message...")
            message = pubsub.get_message(timeout=1.0)
            if message and message["type"] == "message":
                try:
                    _process_redis_message(message)
                except (json.JSONDecodeError, KeyError) as e:
                    logger.error("Error processing Redis message: %s", e, exc_info=True)
        except (RedisConnectionError, RedisTimeoutError) as e:
            logger.error("Error in Redis subscriber loop: %s", e, exc_info=True)
            break


def redis_subscriber() -> None:
    """Subscribe to Redis channel and forward messages to websocket clients.

    Uses a persistent thread-local event loop via run_async() to prevent
    memory leaks from creating new event loops per message.
    """
    while True:
        try:
            redis_client = get_redis_client()
            pubsub = redis_client.pubsub()
            _subscribe_and_listen(pubsub)
        except (RedisConnectionError, RedisTimeoutError, OSError) as e:
            logger.error("Redis subscriber error: %s", e, exc_info=True)
            logger.info("Waiting 5 seconds before reconnecting...")
            time.sleep(5)


def start_redis_subscriber_thread() -> threading.Thread:
    """Start the Redis subscriber in a background thread.

    This is intentionally not started at import-time to avoid unexpected
    background threads in webserver processes.
    """
    logger.info("Starting Redis subscriber thread...")
    subscriber_thread = threading.Thread(target=redis_subscriber, daemon=True)
    subscriber_thread.start()
    logger.info("Redis subscriber thread started")
    return subscriber_thread
