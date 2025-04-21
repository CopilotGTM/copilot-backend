from app_layer.utils.socket_app import sio
from app_layer.utils.logger import get_logger
from uuid import UUID
from socketio.exceptions import ConnectionRefusedError
from sqlmodel import select
from core.redis_client import store_socket_connection, remove_socket_connection
from core.dto.authentication import Authentication
from core.database import get_master_session
from contextlib import contextmanager
from models.db_models import UserCredential
import pprint
from http.cookies import SimpleCookie
import json
import urllib.parse

logger = get_logger()
auth = Authentication()
WEBSOCKET_NAMESPACE = "/websocket/v1"

# Wrap the get_master_session generator so it can be used as a context manager
MasterSession = contextmanager(get_master_session)


@sio.event(namespace=WEBSOCKET_NAMESPACE)
async def connect(sid, environ):
    """
    Handle WebSocket connection for premium users:
      1. Extract JWT from auth-storage cookie
      2. Decode & validate it
      3. Lookup user in DB
      4. Save IDs to socket session & environ
      5. Store the connection in Redis
    """
    try:
        logger.info(f"WebSocket connection attempt: sid={sid}")
        logger.debug("Full WSGI environ:\n%s", pprint.pformat(environ))

        # Extract and log all HTTP_-prefixed headers
        http_headers = {k: v for k, v in environ.items() if k.startswith("HTTP_")}
        logger.info("Extracted HTTP headers:\n%s", pprint.pformat(http_headers))

        # 1. Extract token from auth-storage cookie
        cookie_header = environ.get("HTTP_COOKIE", "")
        token = None
        if cookie_header:
            cookies = SimpleCookie()
            cookies.load(cookie_header)
            raw = cookies.get("auth-storage")
            if raw:
                decoded = urllib.parse.unquote(raw.value)
                try:
                    data = json.loads(decoded)
                    token = data.get("state", {}).get("auth_token")
                    logger.info(f"Extracted token from cookie: {token}")
                except json.JSONDecodeError:
                    logger.warning("Could not decode auth-storage cookie as JSON")

        if not token:
            logger.warning("Connect refused: no auth token provided in cookies")
            raise ConnectionRefusedError("Unauthorized")

        # 2. Decode & basic validate
        payload = auth.decode_for_ws(token)
        logger.info(f"WebSocket connection attempt: sid={sid}, payload={payload}")
        user_id_str = payload.get("user_id")
        org_id_str = payload.get("organisation_id")
        logger.info(f"WebSocket connection attempt: sid={sid}, user_id_str={user_id_str}, org_id_str={org_id_str}")
        if not user_id_str or not org_id_str:
            logger.error("Connect refused: token missing user_id/org_id")
            raise ConnectionRefusedError("Invalid authentication payload")

        user_id = UUID(user_id_str)
        organisation_id = UUID(org_id_str)

        # 3. Verify user exists in DB
        stmt = select(UserCredential).where(
            UserCredential.user_id == user_id,
            UserCredential.organisation_id == organisation_id,
        )
        with MasterSession() as db:
            user = db.exec(stmt).one_or_none()
        if not user:
            logger.warning(f"Connect refused: no matching user in DB (user_id={user_id})")
            raise ConnectionRefusedError("User not found")

        # 4. Save to Socket.IO session
        await sio.save_session(
            sid,
            {"user_id": str(user_id), "organisation_id": str(organisation_id)},
        )

        # 5. Inject into environ for Redis persistence
        environ["user_id"] = str(user_id)
        environ["organisation_id"] = str(organisation_id)

        # 6. Store connection in Redis
        connection_data = await store_socket_connection(sid, environ)
        logger.info(f"WebSocket connection stored in Redis: {connection_data}")

    except ConnectionRefusedError as e:
        logger.error(f"Connection refused: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected connect error: {e}", exc_info=True)
        raise ConnectionRefusedError("Unauthorized")


@sio.event(namespace=WEBSOCKET_NAMESPACE)
async def disconnect(sid):
    """
    Handle WebSocket disconnection for premium users:
      - Remove the connection record from Redis
    """
    logger.info(f"WebSocket disconnect: sid={sid}")
    try:
        count = await remove_socket_connection(sid)
        if count:
            logger.info(f"Removed Redis connection for sid={sid}")
        else:
            logger.warning(f"No Redis connection to remove for sid={sid}")
    except Exception as e:
        logger.error(f"Error during disconnect cleanup for sid={sid}: {e}")
