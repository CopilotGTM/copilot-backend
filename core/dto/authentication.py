# core/dto/authentication.py
from typing import Dict, Any, Optional
from fastapi import Depends, HTTPException, status, Cookie
from sqlmodel import Session, select
import jwt
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
        Decode and validate JWT, raising 403 on error.
        """
        logger.debug("Decoding token...")
        try:
            payload = jwt.decode(token, self.secret_key, algorithms=[self.algorithm])
            logger.debug("Token decoded successfully: %s", payload)
            return payload
        except jwt.PyJWTError as e:
            logger.error("Failed to decode token: %s", str(e))
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or expired authentication token")

    async def verify_session(
        self,
        *,
        session: Session = Depends(get_master_session),
        auth_token: Optional[str] = Cookie(None, alias=cookie_name),
    ) -> Dict[str, Any]:
        """
        FastAPI dependency: verifies the auth_token cookie, checks user exists,
        and returns the decoded payload with user_id and organisation_id.
        Raises HTTP 403 if missing/invalid.
        """
        logger.info("Verifying session...")
        if not auth_token:
            logger.warning("Authentication token is missing")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Authentication token missing")

        logger.debug("Authentication token found, decoding...")
        payload = self.decode_token(auth_token)
        user_id = payload.get("user_id")
        org_id = payload.get("organisation_id")

        if not user_id or not org_id:
            logger.warning("Invalid token payload: user_id or organisation_id missing")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid token payload")

        logger.debug("Token payload validated: user_id=%s, organisation_id=%s", user_id, org_id)

        # verify user actually exists in DB
        logger.debug("Checking if user exists in the database: user_id=%s", user_id)
        stmt = select(UserCredential).where(
            UserCredential.user_id == user_id,
            UserCredential.organisation_id == org_id
        )
        user = session.exec(stmt).one_or_none()
        if not user:
            logger.warning("User not found in the database: user_id=%s, org_id=%s", user_id, org_id)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User not found"
            )
        logger.info("Session verified successfully for user_id=%s", user_id)
        return {"user_id": user_id, "organisation_id": org_id}
