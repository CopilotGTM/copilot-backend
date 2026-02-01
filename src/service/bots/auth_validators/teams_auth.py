"""Teams authentication utilities for bot webhooks."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
import jwt
from jwt import InvalidTokenError
from jwt.algorithms import RSAAlgorithm
from src.infra.config.configs import teams_config
from src.infra.utils.logger import get_logger
from src.service.bots.auth_validators.base import AuthValidationResult, WebhookAuthValidator

logger = get_logger(__name__)


@dataclass
class _OpenIdCache:
    """Cached OpenID config + JWKS."""

    openid_config: dict[str, Any] | None = None
    jwks: dict[str, Any] | None = None
    fetched_at: datetime | None = None


class TeamsAuthValidator(WebhookAuthValidator):
    """Validate Teams Bot Framework JWTs."""

    def __init__(
        self,
        app_id: str,
        openid_config_url: str | None = None,
        allowed_issuers: list[str] | None = None,
    ) -> None:
        self._app_id = app_id
        self._openid_config_url = openid_config_url or teams_config.TEAMS_BOT_OPENID_CONFIG_URL
        self._allowed_issuers = allowed_issuers or teams_config.TEAMS_BOT_ALLOWED_ISSUERS
        self._cache = _OpenIdCache()
        self._lock = asyncio.Lock()

    async def validate(
        self, headers: Mapping[str, str], _body: bytes | None = None
    ) -> AuthValidationResult:
        """Validate Teams auth header."""
        auth_header = headers.get("Authorization", "")
        token = self._extract_bearer_token(auth_header)
        if not token:
            return AuthValidationResult(False, "missing_bearer_token")
        return await self._validate_token(token)

    async def validate_auth_header(self, auth_header: str) -> AuthValidationResult:
        """Validate Teams Authorization header."""
        return await self.validate({"Authorization": auth_header})

    async def _validate_token(self, token: str) -> AuthValidationResult:
        """Validate a JWT token against Bot Framework signing keys."""
        if not self._openid_config_url:
            logger.error("Teams OpenID config URL not configured")
            return AuthValidationResult(False, "openid_config_missing")

        try:
            header = jwt.get_unverified_header(token)
        except InvalidTokenError:
            return AuthValidationResult(False, "invalid_header")

        kid = header.get("kid")
        if not kid:
            return AuthValidationResult(False, "missing_kid")

        signing_key = await self._get_signing_key(kid)
        if signing_key is None:
            return AuthValidationResult(False, "signing_key_not_found")

        try:
            payload = jwt.decode(
                token,
                key=signing_key,
                algorithms=["RS256"],
                options={"verify_aud": False, "verify_iss": False},
                leeway=teams_config.TEAMS_BOT_AUTH_CLOCK_SKEW_SECONDS,
            )
        except InvalidTokenError:
            return AuthValidationResult(False, "invalid_token")

        if not self._is_valid_audience(payload):
            return AuthValidationResult(False, "invalid_audience")

        if not self._is_valid_issuer(payload):
            return AuthValidationResult(False, "invalid_issuer")

        return AuthValidationResult(True, payload=payload)

    async def _get_signing_key(self, kid: str) -> Any | None:
        """Resolve signing key by KID from JWKS."""
        jwks = await self._get_jwks()
        keys = jwks.get("keys", []) if jwks else []
        for key in keys:
            if key.get("kid") == kid:
                try:
                    return RSAAlgorithm.from_jwk(json.dumps(key))
                except Exception:
                    logger.warning("Failed to parse JWKS key for kid=%s", kid)
                    return None
        return None

    async def _get_jwks(self) -> dict[str, Any] | None:
        """Fetch JWKS, using cache when valid."""
        if self._cache_is_valid() and self._cache.jwks:
            return self._cache.jwks

        async with self._lock:
            if self._cache_is_valid() and self._cache.jwks:
                return self._cache.jwks
            await self._refresh_openid_cache()
            return self._cache.jwks

    def _cache_is_valid(self) -> bool:
        if not self._cache.fetched_at:
            return False
        ttl = timedelta(seconds=teams_config.TEAMS_BOT_AUTH_JWKS_CACHE_TTL_SECONDS)
        return datetime.now(UTC) - self._cache.fetched_at < ttl

    async def _refresh_openid_cache(self) -> None:
        """Refresh OpenID config and JWKS from Bot Framework."""
        if not self._openid_config_url:
            return

        timeout = aiohttp.ClientTimeout(total=teams_config.TEAMS_BOT_AUTH_HTTP_TIMEOUT_SEC)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self._openid_config_url) as response:
                    if response.status != 200:
                        logger.warning(
                            "Failed to fetch Teams OpenID config: status=%s",
                            response.status,
                        )
                        return
                    self._cache.openid_config = await response.json()

                jwks_uri = (self._cache.openid_config or {}).get("jwks_uri")
                if not jwks_uri:
                    logger.warning("Teams OpenID config missing jwks_uri")
                    return

                async with session.get(jwks_uri) as response:
                    if response.status != 200:
                        logger.warning(
                            "Failed to fetch Teams JWKS: status=%s",
                            response.status,
                        )
                        return
                    self._cache.jwks = await response.json()

            self._cache.fetched_at = datetime.now(UTC)
        except Exception as exc:
            logger.warning("Failed to refresh Teams JWKS: %s", exc)

    @staticmethod
    def _extract_bearer_token(auth_header: str) -> str | None:
        if not auth_header:
            return None
        parts = auth_header.strip().split(" ", 1)
        if len(parts) != 2:
            return None
        if parts[0].lower() != "bearer":
            return None
        token = parts[1].strip()
        return token or None

    def _is_valid_audience(self, payload: dict[str, Any]) -> bool:
        aud = payload.get("aud")
        if not aud:
            return False
        allowed = {self._app_id, f"api://{self._app_id}"}
        if isinstance(aud, list):
            return any(item in allowed for item in aud)
        return aud in allowed

    def _is_valid_issuer(self, payload: dict[str, Any]) -> bool:
        iss = payload.get("iss")
        if not iss:
            return False
        return iss in self._allowed_issuers
