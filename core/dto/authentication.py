# core/dto/authentication.py
from typing import Dict, Any, Optional
from fastapi import Depends, HTTPException, status, Cookie
from sqlmodel import Session, select
import jwt
from models.db_models import UserCredential
from core.app_state import fastapp
from app_layer.utils.logger import get_logger

get_db = lambda: fastapp.state.master_session()
logger = get_logger()


class Authentication:
    cookie_name: str = "auth_token"
    secret_key: str = "your_secret_key"
    algorithm: str = "HS256"

    def decode_token(self, token: str) -> Dict[str, Any]:
        """
        Decode and validate JWT, raising 403 on error.
        """
        try:
            payload = jwt.decode(token, self.secret_key, algorithms=[self.algorithm])
            return payload
        except jwt.PyJWTError:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or expired authentication token")

    async def verify_session(
        self,
        *,
        session: Session = Depends(get_db),
        auth_token: Optional[str] = Cookie(None, alias=cookie_name),
    ) -> Dict[str, Any]:
        """
        FastAPI dependency: verifies the auth_token cookie, checks user exists,
        and returns the decoded payload with user_id and organisation_id.
        Raises HTTP 403 if missing/invalid.
        """
        logger.info("Verifying session...")
        if not auth_token:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Authentication token missing")

        payload = self.decode_token(auth_token)
        user_id = payload.get("user_id")
        org_id = payload.get("organisation_id")

        if not user_id or not org_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid token payload")

        # verify user actually exists in DB
        user = session.get(UserCredential, user_id)
        if not user:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User not found")

        # everything checks out
        return {"user_id": user_id, "organisation_id": org_id}
