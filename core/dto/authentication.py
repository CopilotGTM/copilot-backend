from typing import Dict, Any, Optional
from fastapi import Depends, HTTPException, status, Cookie
from sqlmodel import Session, select
import jwt
from uuid import UUID
from socketio.exceptions import ConnectionRefusedError
from models.db_models import UserCredential
from core.app_state import fastapp
from app_layer.utils.logger import get_logger
from core.database import get_master_session

logger = get_logger()


class Authentication:
    cookie_name: str = "auth_token"
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
        session: Session = Depends(get_master_session),
        auth_token: Optional[str] = Cookie(None, alias=cookie_name),
    ) -> Dict[str, Any]:
        """
        FastAPI dependency: verifies the auth_token cookie for HTTP routes.
        """
        logger.info("Verifying HTTP session...")
        if not auth_token:
            logger.warning("Authentication token is missing")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Authentication token missing")

        payload = self.decode_token(auth_token)
        user_id = payload.get("user_id")
        org_id = payload.get("organisation_id")

        if not user_id or not org_id:
            logger.warning("Invalid token payload: user_id or organisation_id missing")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid token payload")

        stmt = select(UserCredential).where(
            UserCredential.user_id == UUID(user_id),
            UserCredential.organisation_id == UUID(org_id),
        )
        user = session.exec(stmt).one_or_none()
        if not user:
            logger.warning("User not found in the database: user_id=%s, org_id=%s", user_id, org_id)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User not found")

        logger.info("HTTP session verified successfully for user_id=%s", user_id)
        return {"user_id": user_id, "organisation_id": org_id}
