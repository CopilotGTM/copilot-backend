"""Bot Authentication State Service.

Manages secure Redis-based state tokens for the bot identity linking flow.
This is a generic service that works for any provider (Teams, Slack, Zoom, etc.).

State tokens are:
- 256-bit entropy (cryptographically secure)
- 5-minute TTL in Redis
- Single-use (atomic get-and-delete)

This enables the OAuth-like flow:
1. User sends message to bot
2. Bot creates state token, stores in Redis
3. Bot sends login URL with state to user
4. User completes Supabase login on web
5. Frontend calls callback with access_token + state
6. Backend validates state (consumes from Redis), verifies token, creates mapping
"""

import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from src.domain.models.master_tables.external_identity_models import ExternalIdentityProvider
from src.infra.config.configs import teams_config
from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)

# Redis key prefix for state tokens
BOT_AUTH_STATE_PREFIX = "bot:auth:state:"

# State token TTL in seconds (5 minutes)
STATE_TOKEN_TTL_SECONDS = 300


@dataclass
class BotAuthState:
    """Container for bot authentication state data.

    This is a generic container that works for any provider.
    Provider-specific context is stored in the `context` field.
    """

    state_token: str
    provider: ExternalIdentityProvider
    external_user_id: str
    external_tenant_id: str | None
    external_user_name: str | None
    context: dict[str, Any] = field(default_factory=dict)  # Provider-specific context
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for Redis storage."""
        return {
            "provider": self.provider.value,
            "external_user_id": self.external_user_id,
            "external_tenant_id": self.external_tenant_id,
            "external_user_name": self.external_user_name,
            "context": self.context,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, state_token: str, data: dict[str, Any]) -> "BotAuthState":
        """Deserialize from dictionary stored in Redis."""
        return cls(
            state_token=state_token,
            provider=ExternalIdentityProvider(data["provider"]),
            external_user_id=data["external_user_id"],
            external_tenant_id=data.get("external_tenant_id"),
            external_user_name=data.get("external_user_name"),
            context=data.get("context", {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )


class BotAuthStateService:
    """Service for managing bot authentication state tokens.

    This service handles the secure state token lifecycle for any provider:
    1. Create state token with provider context
    2. Store in Redis with 5-minute TTL
    3. Generate login URL with state parameter
    4. Validate and consume state token (single-use)
    """

    def __init__(self, login_url_base: str | None = None):
        """Initialize the service.

        Args:
            login_url_base: Base URL for the login page. If not provided,
                           falls back to TEAMS_AUTH_LOGIN_URL for backwards compatibility.
        """
        self._login_url_base = login_url_base

    @property
    def login_url_base(self) -> str:
        """Get the login URL base, with fallback to Teams config."""
        if self._login_url_base:
            return self._login_url_base
        # Fallback for backwards compatibility
        return teams_config.TEAMS_AUTH_LOGIN_URL or ""

    def create_state(
        self,
        provider: ExternalIdentityProvider,
        external_user_id: str,
        external_tenant_id: str | None = None,
        external_user_name: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> BotAuthState:
        """Create a new state token and store in Redis.

        Args:
            provider: External provider (teams, slack, etc.)
            external_user_id: User ID from external provider
            external_tenant_id: Tenant/workspace ID (optional)
            external_user_name: Display name from provider (optional)
            context: Provider-specific context (e.g., service_url, conversation_id)

        Returns:
            BotAuthState with the generated token and context
        """
        # Generate 256-bit entropy state token (43 chars URL-safe base64)
        state_token = secrets.token_urlsafe(32)

        state = BotAuthState(
            state_token=state_token,
            provider=provider,
            external_user_id=external_user_id,
            external_tenant_id=external_tenant_id,
            external_user_name=external_user_name,
            context=context or {},
        )

        # Store in Redis with TTL
        redis_key = f"{BOT_AUTH_STATE_PREFIX}{state_token}"
        redis_client = get_redis_client()

        redis_client.setex(
            redis_key,
            STATE_TOKEN_TTL_SECONDS,
            json.dumps(state.to_dict()),
        )

        logger.info(
            "Created %s auth state token for user %s",
            provider.value,
            external_user_id[:20],
        )

        return state

    def peek_state(self, state_token: str) -> BotAuthState | None:
        """Read a state token without consuming it.

        This is used by the frontend to check if a state token is still valid
        before showing the login form. Does NOT delete the token.

        Args:
            state_token: The state token to check

        Returns:
            BotAuthState if valid, None if invalid/expired/already used
        """
        if not state_token:
            return None

        redis_key = f"{BOT_AUTH_STATE_PREFIX}{state_token}"
        redis_client = get_redis_client()

        state_data: bytes | str | None = redis_client.get(redis_key)  # type: ignore[assignment]
        if not state_data:
            return None

        try:
            data = json.loads(state_data)
            return BotAuthState.from_dict(state_token, data)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error("Failed to parse state token data: %s", e)
            return None

    def get_state_ttl(self, state_token: str) -> int:
        """Get the remaining TTL (in seconds) for a state token.

        Args:
            state_token: The state token to check

        Returns:
            Remaining TTL in seconds, or 0 if expired/not found
        """
        if not state_token:
            return 0

        redis_key = f"{BOT_AUTH_STATE_PREFIX}{state_token}"
        redis_client = get_redis_client()

        ttl: int = redis_client.ttl(redis_key)  # type: ignore[assignment]
        # Redis returns -2 if key doesn't exist, -1 if no TTL set
        return max(ttl, 0)

    def validate_and_consume_state(self, state_token: str) -> BotAuthState | None:
        """Validate and consume a state token (single-use).

        This uses atomic GET+DELETE to ensure the token can only be used once.

        Args:
            state_token: The state token to validate

        Returns:
            BotAuthState if valid, None if invalid/expired/already used
        """
        if not state_token:
            logger.warning("Empty state token provided")
            return None

        redis_key = f"{BOT_AUTH_STATE_PREFIX}{state_token}"
        redis_client = get_redis_client()

        # Atomic get and delete using pipeline
        pipe = redis_client.pipeline()
        pipe.get(redis_key)
        pipe.delete(redis_key)
        results = pipe.execute()

        state_data: bytes | str | None = results[0]  # GET result
        deleted: int = results[1]  # DELETE result (number of keys deleted)

        if not state_data:
            logger.warning("State token not found or expired: %s...", state_token[:10])
            return None

        if not deleted:
            # Shouldn't happen if GET returned data, but be defensive
            logger.warning("State token already consumed: %s...", state_token[:10])
            return None

        try:
            data = json.loads(state_data)
            state = BotAuthState.from_dict(state_token, data)

            logger.info(
                "Consumed %s auth state token for user %s",
                state.provider.value,
                state.external_user_id[:20],
            )

            return state
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error("Failed to parse state token data: %s", e)
            return None

    def generate_login_url(self, state: BotAuthState) -> str:
        """Generate the login URL with state parameter.

        Args:
            state: The BotAuthState containing the token

        Returns:
            Full login URL with state and provider query parameters
        """
        base_url = self.login_url_base

        if not base_url:
            logger.error("Login URL base not configured")
            return ""

        # Add state and provider as query parameters
        params = {
            "state": state.state_token,
            "provider": state.provider.value,
        }
        separator = "&" if "?" in base_url else "?"

        return f"{base_url}{separator}{urlencode(params)}"


# Module-level singleton instances per provider
_bot_auth_state_services: dict[str, BotAuthStateService] = {}


def get_bot_auth_state_service(login_url_base: str | None = None) -> BotAuthStateService:
    """Get a BotAuthStateService instance.

    Args:
        login_url_base: Optional base URL for login. Uses default if not provided.

    Returns:
        BotAuthStateService instance
    """
    key = login_url_base or "default"
    if key not in _bot_auth_state_services:
        _bot_auth_state_services[key] = BotAuthStateService(login_url_base)
    return _bot_auth_state_services[key]
