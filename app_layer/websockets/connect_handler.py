# app/websockets/premium_v1/premium_handlers.py
from app_layer.utils.socket_app import sio
from app_layer.utils.logger import get_logger
from http.cookies import SimpleCookie
import uuid
from socketio.exceptions import ConnectionRefusedError
from urllib.parse import parse_qs
from core.redis_client import store_socket_connection
from core.app_state import fastapp

logger = get_logger()

# Define constants
WEBSOCKET_NAMESPACE = "/websocket/v1"


@sio.event(namespace=WEBSOCKET_NAMESPACE)
async def connect(sid, environ):
    """Handle WebSocket connection for premium users."""
    try:
        logger.info(f"WebSocket connection attempt: {sid}")

        # Store connection in Redis
        connection_data = await store_socket_connection(sid, environ)
        logger.info(f"WebSocket connection stored in Redis: {connection_data}")

    except Exception as e:
        logger.error(f"WebSocket connection error: {e}")
        raise ConnectionRefusedError("Unauthorized")


@sio.event(namespace=WEBSOCKET_NAMESPACE)
async def disconnect(sid):
    """Handle WebSocket disconnection"""
    try:
        # Get Redis client from FastAPI app state
        redis_client = fastapp.state.redis_client

        # Remove connection data from Redis
        connection_key = f"websocket:connections:{sid}"
        redis_client.delete(connection_key)
        logger.info(f"WebSocket connection removed from Redis: {sid}")
    except Exception as e:
        logger.error(f"Error during disconnect: {e}")
