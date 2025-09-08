# core/redis_client.py

import redis
from typing import Optional
from ..config.configs import Config
from ..utils.logger import get_logger

logger = get_logger()


class RedisConnectionManager:
    """Singleton Redis connection pool manager."""

    _instance: Optional["RedisConnectionManager"] = None
    _sync_pool: Optional[redis.ConnectionPool] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(RedisConnectionManager, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if self._sync_pool is None:
            self._initialize_pool()

    def _initialize_pool(self) -> None:
        """Initialize Redis connection pool."""
        config = Config()
        redis_host, redis_port, redis_password = config.get_redis_client_details
        ssl_enabled = False if config.env == "local" else True

        # Build connection parameters
        connection_params = {
            "host": redis_host,
            "port": redis_port,
            "password": redis_password,
            "decode_responses": True,
            "socket_timeout": 30,
            "socket_connect_timeout": 30,
            "retry_on_timeout": True,
            "health_check_interval": 30,
            "socket_keepalive": True,
            "max_connections": 20,  # Connection pool size
        }

        # Add SSL configuration only for non-local environments
        if ssl_enabled:
            connection_params["ssl"] = True
            connection_params["ssl_check_hostname"] = False

        self._sync_pool = redis.ConnectionPool(**connection_params)

        # Test the connection
        test_client = redis.Redis(connection_pool=self._sync_pool)
        try:
            test_client.ping()
            logger.info("Redis connection pool initialized successfully")
        except Exception as e:
            logger.error("Failed to initialize Redis connection pool: %s", e)
            raise

    def get_client(self) -> redis.Redis:
        """Get a Redis client from the connection pool."""
        if self._sync_pool is None:
            self._initialize_pool()
        return redis.Redis(connection_pool=self._sync_pool)

    def close_pool(self) -> None:
        """Close the connection pool."""
        if self._sync_pool:
            self._sync_pool.disconnect()
            self._sync_pool = None
            logger.info("Redis connection pool closed")


# Global connection manager instance
_redis_manager = RedisConnectionManager()


def get_redis_client() -> redis.Redis:
    """
    Return a Redis client instance from the connection pool.
    """
    return _redis_manager.get_client()


def close_redis_pool() -> None:
    """
    Close the Redis connection pool.
    """
    _redis_manager.close_pool()
