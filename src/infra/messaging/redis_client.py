# core/redis_client.py

import os
import ssl as ssl_mod
from typing import Optional

import redis

from ..config.configs import Config
from ..utils.logger import get_logger

logger = get_logger()


class RedisConnectionManager:
    """Singleton Redis connection pool manager."""

    _instance: Optional["RedisConnectionManager"] = None
    _sync_pool: redis.ConnectionPool | None = None
    _initialized: bool = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # Do not eagerly initialize the pool at import time.
        # Initialize lazily on first use or explicitly via initialize().
        pass

    def initialize(self) -> None:
        """
        Explicitly initialize the Redis connection pool.
        Safe to call multiple times - will only initialize once.
        Call this during application/worker startup.
        """
        if not self._initialized:
            self._initialize_pool()
            self._initialized = True

    def _initialize_pool(self) -> None:
        """Initialize Redis connection pool."""
        config = Config()
        redis_host, redis_port, redis_password = config.get_redis_client_details
        ssl_enabled = config.env != "local"

        # Base connection parameters (compatible across redis-py versions)
        connection_params: dict = {
            "host": redis_host,
            "port": redis_port,
            "password": redis_password,
            "decode_responses": True,
            "socket_timeout": 30,
            "socket_connect_timeout": 30,
            "retry_on_timeout": True,
            "health_check_interval": 30,
            "socket_keepalive": True,
            "max_connections": 20,
        }

        # Prefer using SSLConnection class for TLS to avoid unsupported 'ssl' kwarg
        # in older redis-py versions. Fall back gracefully if unavailable.
        if ssl_enabled:
            try:
                from redis.connection import SSLConnection  # type: ignore

                # Configure certificate requirements from env, default CERT_NONE
                ssl_cert_reqs_str = os.environ.get("REDIS_SSL_CERT_REQS", "CERT_NONE")
                ssl_cert_reqs_map = {
                    "CERT_NONE": ssl_mod.CERT_NONE,
                    "CERT_OPTIONAL": ssl_mod.CERT_OPTIONAL,
                    "CERT_REQUIRED": ssl_mod.CERT_REQUIRED,
                }
                connection_params["connection_class"] = SSLConnection
                connection_params["ssl_cert_reqs"] = ssl_cert_reqs_map.get(
                    ssl_cert_reqs_str, ssl_mod.CERT_NONE
                )
            except Exception:
                # As a last resort, try the 'ssl' kwarg; some environments may still accept it.
                # Do NOT pass unsupported options like 'ssl_check_hostname'.
                connection_params["ssl"] = True  # type: ignore[assignment]

        self._sync_pool = redis.ConnectionPool(**connection_params)

        # Test the connection on first init
        test_client = redis.Redis(connection_pool=self._sync_pool)
        try:
            test_client.ping()
            logger.debug(
                "Redis connection pool initialized successfully (host=%s, ssl=%s)",
                redis_host,
                ssl_enabled,
            )
        except Exception as e:
            logger.error("Failed to initialize Redis connection pool: %s", e)
            # Ensure we don't keep a broken pool around
            try:
                self._sync_pool.disconnect()  # type: ignore[union-attr]
            except Exception:
                pass
            self._sync_pool = None
            self._initialized = False
            raise

    def get_client(self) -> redis.Redis:
        """Get a Redis client from the connection pool."""
        if self._sync_pool is None:
            self._initialize_pool()
            self._initialized = True
        return redis.Redis(connection_pool=self._sync_pool)

    def close_pool(self) -> None:
        """Close the connection pool."""
        if self._sync_pool:
            self._sync_pool.disconnect()
            self._sync_pool = None
            self._initialized = False
            logger.debug("Redis connection pool closed")


# Lazy, module-level singleton (do not initialize at import time)
_redis_manager: RedisConnectionManager | None = None


def initialize_redis_pool() -> None:
    """
    Initialize the Redis connection pool at application startup.
    Safe to call multiple times - will only initialize once.
    """
    global _redis_manager
    if _redis_manager is None:
        _redis_manager = RedisConnectionManager()
    _redis_manager.initialize()


def get_redis_client() -> redis.Redis:
    """
    Return a Redis client instance from the connection pool.
    """
    global _redis_manager
    if _redis_manager is None:
        _redis_manager = RedisConnectionManager()
    return _redis_manager.get_client()


def close_redis_pool() -> None:
    """
    Close the Redis connection pool.
    """
    global _redis_manager
    if _redis_manager is not None:
        _redis_manager.close_pool()
        _redis_manager = None
