from contextlib import contextmanager
from uuid import UUID

from socketio.exceptions import ConnectionRefusedError
from sqlmodel import select
from src.api.dependencies.master_dependencies import acquire_master_session
from src.api.events.socket_app import sio
from src.domain.models.authentication import Authentication
from src.domain.models.master_tables import User
from src.infra.utils.logger import get_logger

logger = get_logger()
auth_class = Authentication()
WEBSOCKET_NAMESPACE = "/websocket/v1"

# Wrap the get_master_session generator so it can be used as a context manager
MasterSession = contextmanager(acquire_master_session)


@sio.event(namespace=WEBSOCKET_NAMESPACE)
async def connect(sid, environ, auth):
    """
    Handle WebSocket connection for premium users:
      1. Extract JWT from auth-storage cookie
      2. Decode & validate it
      3. Lookup user in DB
      4. Save IDs to environ for session use
    """
    logger.info(f"WebSocket connection attempt: sid={sid}")

    token = auth.get("token") if auth else environ.get("HTTP_TOKEN")
    if not token:
        logger.warning("Connect refused: no auth token provided in cookies")
        raise ConnectionRefusedError("Unauthorized")

    # Decode & basic validate
    payload = auth_class.decode_for_ws(token)
    user_id_str = payload.get("user_id")
    org_id_str = payload.get("organisation_id")
    logger.info(
        f"WebSocket connection attempt: sid={sid}, user_id_str={user_id_str}, org_id_str={org_id_str}"
    )

    if not user_id_str or not org_id_str:
        logger.error("Connect refused: token missing user_id/org_id")
        raise ConnectionRefusedError("Invalid authentication payload")

    user_id = UUID(user_id_str)
    organisation_id = UUID(org_id_str)
    logger.info(f"Parsed UUIDs: user_id={user_id}, organisation_id={organisation_id}")

    # Verify user exists in DB using new User model
    stmt = select(User).where(
        User.user_id == user_id,
        User.organisation_id == organisation_id,
    )
    with MasterSession() as session:
        user = session.exec(stmt).one_or_none()

    if not user:
        logger.warning(f"Connect refused: no matching user in DB (user_id={user_id})")
        raise ConnectionRefusedError("User not found")

    # Inject into environ for session use
    environ["user_id"] = str(user_id)
    environ["organisation_id"] = str(organisation_id)

    logger.info(
        f"WebSocket connection established: sid={sid}, user_id={user_id}, organisation_id={organisation_id}"
    )


@sio.event(namespace=WEBSOCKET_NAMESPACE)
async def disconnect(sid):
    """
    Handle WebSocket disconnection.
    """
    logger.info(f"WebSocket disconnection: sid={sid}")
