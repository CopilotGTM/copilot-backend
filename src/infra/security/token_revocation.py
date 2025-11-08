from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Final

import jwt
from jwt import InvalidTokenError
from redis import Redis
from src.infra.config.configs import supabase_config
from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.logger import get_logger

logger = get_logger("token_revocation")

_TOKEN_PREFIX: Final[str] = "security:revoked-token:"
_USER_PREFIX: Final[str] = "security:revoked-admin:"
_DEFAULT_TOKEN_TTL_SECONDS: Final[int] = 3600
_MIN_TOKEN_TTL_SECONDS: Final[int] = 60
_DEFAULT_USER_TTL_SECONDS: Final[int] = 3600


class TokenRevocationService:
    """Manage revoked Supabase access tokens and user-wide session blocks."""

    def __init__(self) -> None:
        self._redis: Redis | None = None
        self._jwt_secret: str = supabase_config.SUPABASE_JWT_SECRET

    def _client(self) -> Redis:
        if self._redis is None:
            self._redis = get_redis_client()
        return self._redis

    def _token_key(self, token_hash: str) -> str:
        return f"{_TOKEN_PREFIX}{token_hash}"

    def _user_key(self, user_id: str) -> str:
        return f"{_USER_PREFIX}{user_id}"

    def _hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _decode_ttl(self, token: str) -> int:
        try:
            payload = jwt.decode(
                token,
                self._jwt_secret,
                algorithms=["HS256"],
                options={"verify_aud": False},
            )
        except InvalidTokenError as exc:
            logger.debug("Failed to decode token for revocation TTL: %s", exc)
            return _DEFAULT_TOKEN_TTL_SECONDS

        exp = payload.get("exp")
        if exp is None:
            return _DEFAULT_TOKEN_TTL_SECONDS

        expiry = datetime.fromtimestamp(exp, tz=UTC)
        ttl = int((expiry - datetime.now(UTC)).total_seconds())
        if ttl <= 0:
            return _MIN_TOKEN_TTL_SECONDS
        return ttl

    def revoke_token(self, token: str) -> None:
        """Store the token hash in Redis so subsequent calls reject it."""
        try:
            ttl = self._decode_ttl(token)
            key = self._token_key(self._hash_token(token))
            self._client().setex(key, ttl, "1")
        except Exception as exc:
            logger.error("Failed to revoke token: %s", exc)

    def revoke_user(self, user_id: str, ttl_seconds: int | None = None) -> None:
        """Block all sessions for a user until the TTL expires or cleared."""
        ttl = ttl_seconds if ttl_seconds and ttl_seconds > 0 else _DEFAULT_USER_TTL_SECONDS
        try:
            self._client().setex(self._user_key(user_id), ttl, "1")
        except Exception as exc:
            logger.error("Failed to revoke sessions for user %s: %s", user_id, exc)

    def clear_user_revocation(self, user_id: str) -> None:
        try:
            self._client().delete(self._user_key(user_id))
        except Exception as exc:
            logger.error("Failed to clear revocation for user %s: %s", user_id, exc)

    def is_token_revoked(self, token: str, user_id: str | None = None) -> bool:
        try:
            client = self._client()
        except Exception as exc:
            logger.error("Unable to access Redis for token revocation check: %s", exc)
            return False

        if user_id and client.exists(self._user_key(user_id)) == 1:
            return True

        token_key = self._token_key(self._hash_token(token))
        return client.exists(token_key) == 1


token_revocation_service = TokenRevocationService()
