# core/redis_client.py

import redis
from core.configs.configs import Config
from core.app_state import fastapp
from app_layer.utils.logger import get_logger
import json
import uuid

config = Config()
logger = get_logger()


def get_redis_client() -> redis.Redis:
    """
    Return the Redis client instance.
    """
    config = Config()
    redis_host, redis_port, redis_password = config.get_redis_client_details
    logger.info(f"Redis Host - {redis_host}")
    redis_client = redis.Redis(
        host=redis_host,
        port=redis_port,
        password=redis_password,
        decode_responses=True,
        socket_timeout=30,
        socket_connect_timeout=30,
        retry_on_timeout=True,
        health_check_interval=30,
        ssl=False,
        socket_keepalive=True
    )
    redis_client.ping()
    return redis_client

async def store_socket_connection(sid, environ):
    """
    Store the connection information in Redis.

    Parameters:
    sid (str): The session ID of the client.
    environ (dict): The environment information to be stored.
    """
    try:
        # Prefix for Redis connection keys
        REDIS_CONNECTION_PREFIX = "websocket:connections:"
        redis_client = fastapp.state.redis_client
        connection_key = f"{REDIS_CONNECTION_PREFIX}{sid}"
        data = {
            "user_id": environ.get("user_id"),
            "organisation_id": environ.get("organisation_id"),
        }
        connection_data = json.dumps(data)
        redis_client.set(connection_key, connection_data)
        redis_client.expire(connection_key, 7200)
    except Exception as e:
        logger.error(f"Error storing WebSocket connection in Redis for sid={sid}: {e}", exc_info=True)
        raise


async def remove_socket_connection(sid):
    """
    Remove the connection information from Redis.

    Parameters:
    sid (str): The session ID of the client.

    Returns:
    int: Number of keys that were removed (0 if none, 1 if removed).
    """
    # Prefix for Redis connection keys
    REDIS_CONNECTION_PREFIX = "websocket:connections:"
    connection_key = f"{REDIS_CONNECTION_PREFIX}{sid}"
    redis_client = fastapp.state.redis_client
    removed_count = redis_client.delete(connection_key)
    if removed_count:
        logger.info(f"Removed WebSocket connection from Redis: {connection_key}")
    else:
        logger.warning(f"No WebSocket connection found to remove for key: {connection_key}")
    return removed_count


async def get_socket_connection(sid):
    """
    Retrieve the connection metadata (and, if stored, the original environ dict)
    for a given WebSocket session ID.
    """
    REDIS_CONNECTION_PREFIX = "websocket:connections:"
    connection_key = f"{REDIS_CONNECTION_PREFIX}{sid}"
    redis_client = fastapp.state.redis_client

    # pull back all fields from the hash
    data = redis_client.get(connection_key)
    if not data:
        logger.warning(f"No data found in Redis for key: {connection_key}")
        return None

    return data
