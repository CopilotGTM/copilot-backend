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
    redis_host, redis_port = config.get_redis_client_details
    logger.info(f"Redis Host - {redis_host}")
    redis_client = redis.Redis(host=redis_host, port=redis_port, decode_responses=True, socket_timeout=5)
    redis_client.ping()
    return redis_client


async def store_socket_connection(sid, environ):
    """
    Store the connection information in Redis.

    Parameters:
    sid (str): The session ID of the client.
    environ (dict): The environment information to be stored.
    """

    # Prefix for Redis connection keys
    REDIS_CONNECTION_PREFIX = "websocket:connections:"
    redis_client = fastapp.state.redis_client
    connection_key = f"{REDIS_CONNECTION_PREFIX}{sid}"
    connection_data = {"sid": sid, "connection_id": str(uuid.uuid4())}
    redis_client.hmset(connection_key, connection_data)
    redis_client.expire(connection_key, 7200)
    return connection_data
