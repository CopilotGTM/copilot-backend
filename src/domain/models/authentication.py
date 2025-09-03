from typing import Dict, Any, Optional
from fastapi import Depends, HTTPException, status, Cookie, Request
from sqlmodel import Session, select
import json
from urllib.parse import unquote
from uuid import UUID
from socketio.exceptions import ConnectionRefusedError
from src.domain.models.master_tables import User
from src.infra.utils.app_state import fastapp
from src.infra.utils.logger import get_logger
from src.api.dependencies.master_dependencies import get_master_session
from src.infra.auth.supabase_admin_client import supabase_admin_client

logger = get_logger()


class Authentication:
    cookie_name: str = "auth-storage"

    async def verify_session(
        self,
        *,
        master_session: Session = Depends(get_master_session),
        auth_storage: Optional[str] = Cookie(None, alias=cookie_name)
    ) -> Dict[str, Any]:
        """
        FastAPI dependency: verifies the auth_token cookie for HTTP routes.
        For Supabase, we'll verify the session token and extract user info.
        """
        logger.debug("Starting session verification...")

        try:
            if not auth_storage:
                logger.warning("Auth-storage cookie is missing")
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Authentication token missing")

            # Parse JSON
            try:
                # URL decode the cookie value
                decoded_storage = unquote(auth_storage)
                logger.debug("Decoded auth-storage cookie")
                storage_data = json.loads(decoded_storage)
            except json.JSONDecodeError as e:
                logger.error("Failed to parse auth-storage JSON: %s", str(e))
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid auth-storage format")

            # Extract Supabase session token
            access_token = storage_data.get("state", {}).get("access_token")
            if not access_token:
                logger.warning("No access_token found in auth-storage")
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Authentication token missing")

            # Get user from Supabase token
            user = supabase_admin_client.get_user_from_token(access_token, master_session)

            if not user:
                logger.warning("User not found or token invalid")
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User not found or token invalid")

            logger.info("HTTP session verified successfully for user_id=%s", user.user_id)
            return {"user_id": str(user.user_id), "organisation_id": str(user.organisation_id), "user": user}

        except HTTPException:
            # Re-raise HTTP exceptions
            raise
        except Exception as e:
            logger.error("Unexpected error during session verification: %s", str(e))
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Authentication error")

    def decode_for_ws(self, token: str) -> Dict[str, Any]:
        """
        Decode and validate token for WebSocket, raising ConnectionRefusedError on error.
        """
        logger.debug("Decoding token (WebSocket)...")
        try:
            # Verify the token with Supabase
            payload = supabase_admin_client.verify_token(token)

            # Extract user information
            supabase_user_id = payload.get("user", {}).get("id")
            if not supabase_user_id:
                logger.warning("No user ID found in WebSocket token")
                raise ConnectionRefusedError("Invalid token payload")

            # For WebSocket, we'll return the basic user info
            # You might want to add a database session here to get full user details
            return {
                "supabase_user_id": supabase_user_id,
                "user_id": payload.get("user_id"),  # If you store this in token
                "organisation_id": payload.get("organisation_id"),  # If you store this in token
            }

        except HTTPException as e:
            logger.error("WebSocket auth failed: %s", str(e))
            raise ConnectionRefusedError("Unauthorized")
        except Exception as e:
            logger.error("WebSocket auth token decode failed: %s", str(e))
            raise ConnectionRefusedError("Unauthorized")
