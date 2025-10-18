import asyncio
import json
import threading
import time

from src.api.events.socket_app import sio
from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.logger import get_logger

logger = get_logger()
WEBSOCKET_NAMESPACE = "/websocket/v1"
REDIS_CHANNEL = "websocket_messages"


async def emit_websocket_message(event, message_data, sid):
    """Emit message to websocket client asynchronously"""
    try:
        await sio.emit(event, message_data, room=sid, namespace=WEBSOCKET_NAMESPACE)
        logger.info(f"Successfully emitted message to websocket client {sid}")
    except Exception as e:
        logger.error(f"Error emitting websocket message: {e}", exc_info=True)


def redis_subscriber():
    """Subscribe to Redis channel and forward messages to websocket clients"""
    while True:
        try:
            redis_client = get_redis_client()
            pubsub = redis_client.pubsub()
            logger.info(f"Subscribing to Redis channel {REDIS_CHANNEL}")
            pubsub.subscribe(REDIS_CHANNEL)
            logger.info(f"Successfully subscribed to Redis channel {REDIS_CHANNEL}")

            while True:
                try:
                    logger.debug("Waiting for message...")
                    message = pubsub.get_message(timeout=1.0)
                    if message and message["type"] == "message":
                        try:
                            data = json.loads(message["data"])
                            sid = data["sid"]
                            event = data["event"]
                            message_data = data["data"]

                            # Create event loop for async operation
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)

                            # Emit message asynchronously
                            loop.run_until_complete(
                                emit_websocket_message(event, message_data, sid)
                            )
                            loop.close()

                        except Exception as e:
                            logger.error(
                                f"Error processing Redis message: {e}",
                                exc_info=True,
                            )
                except Exception as e:
                    logger.error(f"Error in Redis subscriber loop: {e}", exc_info=True)
                    break

        except Exception as e:
            logger.error(f"Redis subscriber error: {e}", exc_info=True)
            logger.info("Waiting 5 seconds before reconnecting...")
            time.sleep(5)  # Wait before reconnecting
            continue


# Start the Redis subscriber in a background thread
logger.info("Starting Redis subscriber thread...")
subscriber_thread = threading.Thread(target=redis_subscriber, daemon=True)
subscriber_thread.start()
logger.info("Redis subscriber thread started")
