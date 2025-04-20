from app_layer.utils.socket_app import sio
from app_layer.utils.logger import get_logger
from http.cookies import SimpleCookie
from uuid import UUID
from socketio.exceptions import ConnectionRefusedError
from urllib.parse import parse_qs

from core.redis_client import store_socket_connection, remove_socket_connection
from core.dto.authentication import Authentication
from core.database import get_master_session
from models.db_models import UserCredential
from sqlmodel import select, Session

logger = get_logger()
auth = Authentication()

# Namespace for all premium websocket events
WEBSOCKET_NAMESPACE = "/websocket/v1"


@sio.event(namespace=WEBSOCKET_NAMESPACE)
async def connect(sid, environ):
    """
    Handle WebSocket connection for premium users:
      1. Extract JWT from cookie or query-string
      2. Decode & validate it
      3. Spin up a DB session and verify the user exists
      4. Save IDs to socket session & environ
      5. Store the connection in Redis
    """
    try:
        logger.info(f"WebSocket connection attempt: sid={sid}")

        # 1. Extract token from cookie or query string
        token = None
        if "HTTP_COOKIE" in environ:
            cookie = SimpleCookie()
            cookie.load(environ["HTTP_COOKIE"])
            morsel = cookie.get(auth.cookie_name)
            if morsel:
                token = morsel.value

        if not token:
            qs = parse_qs(environ.get("QUERY_STRING", ""))
            token = qs.get("token", [None])[0]

        if not token:
            logger.warning("Connect refused: no auth token provided")
            raise ConnectionRefusedError("Unauthorized")

        # 2. Decode & basic validate
        payload = auth.decode_token(token)
        user_id_str = payload.get("user_id")
        org_id_str = payload.get("organisation_id")
        if not user_id_str or not org_id_str:
            logger.error("Connect refused: token missing user_id/org_id")
            raise ConnectionRefusedError("Invalid authentication payload")

        # 3. Parse UUIDs and verify user in DB
        try:
            user_id = UUID(user_id_str)
            organisation_id = UUID(org_id_str)
        except (ValueError, TypeError):
            logger.error("Connect refused: malformed UUIDs in token payload")
            raise ConnectionRefusedError("Invalid authentication payload")

        # Manually get a master session (get_master_session is a generator)
        session_gen = get_master_session()
        db: Session = next(session_gen)
        try:
            stmt = select(UserCredential).where(
                UserCredential.user_id == user_id_str,
                UserCredential.organisation_id == org_id_str,
            )
            user = db.exec(stmt).one_or_none()
            if not user:
                logger.warning("Connect refused: no matching user in DB (user_id=%s)", user_id)
                raise ConnectionRefusedError("User not found")
        finally:
            # close out the dependency
            try:
                session_gen.close()
            except Exception:
                pass

        # 4a. Save to Socket.IO session
        await sio.save_session(
            sid,
            {
                "user_id": str(user_id),
                "organisation_id": str(organisation_id),
            },
        )

        # 4b. Also inject into environ for Redis persistence
        environ["user_id"] = str(user_id)
        environ["organisation_id"] = str(organisation_id)

        # 5. Store connection in Redis
        connection_data = await store_socket_connection(sid, environ)
        logger.info(f"WebSocket connection stored in Redis: {connection_data}")

    except ConnectionRefusedError as cre:
        logger.error(f"ConnectionRefusedError: {cre}")
        # must re‑raise to tell Socket.IO to refuse
        raise cre
    except Exception as e:
        logger.error(f"Unexpected connect error: {e}")
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
