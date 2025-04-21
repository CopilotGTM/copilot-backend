from typing import Dict, Any, Optional
from fastapi import Depends, HTTPException, status, Cookie, Request
from sqlmodel import Session, select
import jwt
import json
from urllib.parse import unquote
from uuid import UUID
from socketio.exceptions import ConnectionRefusedError
from models.db_models import UserCredential
from core.app_state import fastapp
from app_layer.utils.logger import get_logger
from core.database import get_master_session

logger = get_logger()


class Authentication:
    cookie_name: str = "auth-storage"
    secret_key: str = "your_secret_key"
    algorithm: str = "HS256"

    def decode_token(self, token: str) -> Dict[str, Any]:
        """
        Decode and validate JWT for HTTP routes, raising HTTPException on error.
        """
        logger.debug("Decoding token (HTTP)...")
        try:
            payload = jwt.decode(token, self.secret_key, algorithms=[self.algorithm])
            logger.debug("Token decoded successfully: %s", payload)
            return payload
        except jwt.PyJWTError as e:
            logger.error("Failed to decode token: %s", str(e))
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or expired authentication token")

    def decode_for_ws(self, token: str) -> Dict[str, Any]:
        """
        Decode and validate JWT for WebSocket, raising ConnectionRefusedError on error.
        """
        logger.debug("Decoding token (WebSocket)...")
        try:
            return jwt.decode(token, self.secret_key, algorithms=[self.algorithm])
        except jwt.PyJWTError as e:
            logger.error("WebSocket auth token decode failed: %s", str(e))
            raise ConnectionRefusedError("Unauthorized")

    async def verify_session(
        self,
        *,
        master_session: Session = Depends(get_master_session),
        auth_storage: Optional[str] = Cookie(None, alias=cookie_name)
    ) -> Dict[str, Any]:
        """
        FastAPI dependency: verifies the auth_token cookie for HTTP routes.
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

            # Extract auth token
            auth_token = storage_data.get("state", {}).get("auth_token")
            if not auth_token:
                logger.warning("No auth_token found in auth-storage")
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Authentication token missing")

            # Decode and validate token
            payload = self.decode_token(auth_token)
            user_id = payload.get("user_id")
            org_id = payload.get("organisation_id")

            if not user_id or not org_id:
                logger.warning("Invalid token payload: user_id or organisation_id missing")
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid token payload")

            # Verify user exists
            try:
                stmt = select(UserCredential).where(
                    UserCredential.user_id == UUID(user_id),
                    UserCredential.organisation_id == UUID(org_id),
                )
                user = master_session.exec(stmt).one_or_none()

                if not user:
                    logger.warning("User not found in the database: user_id=%s, org_id=%s", user_id, org_id)
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User not found")

                logger.info("HTTP session verified successfully for user_id=%s", user_id)
                return {"user_id": user_id, "organisation_id": org_id}

            except ValueError as e:
                logger.error("Invalid UUID format: %s", str(e))
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user or organisation ID format"
                )

        except Exception as e:
            logger.error("Unexpected error during session verification: %s", str(e))
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Authentication error")
