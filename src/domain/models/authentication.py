import json
from typing import Any
from urllib.parse import unquote

from fastapi import Cookie, Depends, HTTPException, status
from sqlmodel import Session
from src.api.dependencies.master_dependencies import get_master_session
from src.infra.auth.supabase_admin_client import supabase_admin_client
from src.infra.utils.logger import get_logger

logger = get_logger()


class Authentication:
    cookie_name: str = "auth-storage"

    def verify_session(
        self,
        *,
        master_session: Session = Depends(get_master_session),
        auth_storage: str | None = Cookie(None, alias=cookie_name),
    ) -> dict[str, Any]:
        """
        FastAPI dependency: verifies the auth_token cookie for HTTP routes.
        For Supabase, we'll verify the session token and extract user info.
        """
        logger.debug("Starting session verification...")

        try:
            if not auth_storage:
                logger.warning("Auth-storage cookie is missing")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Authentication token missing",
                )

            # Parse JSON
            try:
                # URL decode the cookie value
                decoded_storage = unquote(auth_storage)
                logger.debug("Decoded auth-storage cookie")
                storage_data = json.loads(decoded_storage)
            except json.JSONDecodeError as e:
                logger.error("Failed to parse auth-storage JSON: %s", str(e))
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid auth-storage format",
                ) from e

            # Extract Supabase session token
            access_token = storage_data.get("state", {}).get("access_token")
            if not access_token:
                logger.warning("No access_token found in auth-storage")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Authentication token missing",
                )

            # Get user from Supabase token
            user = supabase_admin_client.get_user_from_token(access_token, master_session)

            if not user:
                logger.warning("User not found or token invalid")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User not found or token invalid",
                )

            logger.info("HTTP session verified successfully for user_id=%s", user.user_id)
            return {
                "user_id": str(user.user_id),
                "organisation_id": str(user.organisation_id),
                "user": user,
            }

        except HTTPException:
            # Re-raise HTTP exceptions
            raise
        except Exception as e:
            logger.error("Unexpected error during session verification: %s", str(e))
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Authentication error",
            ) from e
