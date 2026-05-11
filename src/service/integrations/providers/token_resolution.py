"""Provider-agnostic OAuth token resolution for sync tasks.

Handles reading tokens from local credentials, proactive refresh before expiry,
and 401 retry logic. Works with any provider registered in ProviderFactory
(Google today, Microsoft/HubSpot/Salesforce later).

Used by Celery sync tasks during the "no session held" phase — tokens are
extracted from DB in the extract phase, then this module manages their lifecycle
during external API calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.utils.logger import get_logger
from src.service.integrations.sync_health import AuthTokenRevokedError

logger = get_logger(__name__)

# Refresh proactively before expiry — OAuth tokens typically last ~60 min
# and a sync can take several minutes of pagination. 5-min buffer prevents
# 401 mid-sync across all providers.
_TOKEN_EXPIRY_BUFFER = timedelta(minutes=5)


@dataclass
class IntegrationTokenInfo:
    """OAuth token info extracted from DB during the sync task's extract phase.

    Provider-agnostic — works with Google, Microsoft, or any future provider.
    All fields are optional because the dataclass is constructed incrementally
    during the DB read, but integration_type, refresh_token, and organisation_id
    must be set before any refresh operation.
    """

    access_token: str | None = None
    refresh_token: str | None = None
    access_token_expires_at: datetime | None = None
    integration_type: IntegrationType | None = None
    organisation_id: UUID | None = None
    integration_id: UUID | None = None
    scopes: list[str] | None = None

    @classmethod
    def from_integration(cls, integration: Any, organisation_id: UUID) -> IntegrationTokenInfo:
        """Build from a KnowledgeIntegration row.

        Accepts Any to avoid importing KnowledgeIntegration (circular import).
        """
        credentials = dict(integration.credentials or {})
        int_type = integration.integration_type
        if isinstance(int_type, str):
            int_type = IntegrationType(int_type)
        return cls(
            access_token=credentials.get("access_token"),
            refresh_token=credentials.get("refresh_token"),
            access_token_expires_at=integration.access_token_expires_at,
            integration_type=int_type,
            organisation_id=organisation_id,
            integration_id=integration.integration_id,
            scopes=list(integration.scopes or []),
        )


def _is_token_expiring_soon(token_info: IntegrationTokenInfo) -> bool:
    """Check if the token needs a proactive refresh."""
    # Slack user tokens can be long-lived when token rotation is not enabled.
    # In that case we keep using the stored token until Slack rejects it.
    if token_info.access_token_expires_at is None:
        return not (
            token_info.integration_type == IntegrationType.SLACK and token_info.access_token
        )
    now = datetime.now(UTC)
    expiry = token_info.access_token_expires_at
    # DB stores tz-naive datetimes — normalize for comparison
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return now + _TOKEN_EXPIRY_BUFFER >= expiry


# Microsoft AADSTS codes that indicate permanent refresh token invalidation.
# Source: https://learn.microsoft.com/en-us/entra/identity-platform/reference-error-codes
_MS_PERMANENT_AUTH_CODES = frozenset(
    {
        50173,  # Password change / credential reset / admin revocation
        65001,  # Admin or user revoked consent
        70000,  # Generic invalid refresh token (malformed, binding mismatch)
        70001,  # App disabled by admin
        70008,  # Refresh token expired due to inactivity
        700082,  # Refresh token expired (detailed variant of 70008)
        700084,  # SPA refresh token fixed lifetime expired
    }
)

# Microsoft AADSTS codes that require interactive re-auth (MFA, Conditional Access).
# Not a token issue per se, but unrecoverable without user interaction.
_MS_INTERACTIVE_REAUTH_CODES = frozenset(
    {
        50076,  # MFA policy change
        50078,  # MFA expired
        50158,  # External security challenge required
        53003,  # Conditional Access blocks token issuance
        530003,  # Device compliance required
    }
)


def _is_permanent_auth_error(exc: Exception) -> bool:
    """Detect non-recoverable auth errors from Google and Microsoft OAuth.

    Returns True when a token refresh fails with an error that will never
    succeed on retry — the user must re-authenticate. Covers:

    Google (all return HTTP 400):
    - invalid_grant: user revoked access, 6-month inactivity, password change,
      max token limit, admin policy, suspicious activity
    - admin_policy_enforced: Workspace admin restricted app scopes
    - invalid_client: client secret rotated (unrecoverable without redeploy)

    Microsoft (all return HTTP 400 with AADSTS codes):
    - 50173/70008/700082/700084: token expired or revoked
    - 65001/70001: consent revoked or app disabled
    - 50076/50078/50158/53003/530003: requires interactive re-auth (MFA/CA)
    """
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 400:
        body = exc.response.text.lower()

        # Google: all permanent auth failures return {"error": "invalid_grant"}
        # with varying error_description (or sometimes no description at all)
        if "invalid_grant" in body:
            return True
        # Google Workspace: admin restricted app scopes
        if "admin_policy_enforced" in body:
            return True
        # Google: client credentials invalid (secret rotated)
        if "invalid_client" in body:
            return True

        # Microsoft: check AADSTS error codes in response body
        # Response contains "error_codes": [70008] array
        if "aadsts" in body or "error_codes" in body:
            return _check_microsoft_error_codes(body)

    # Fallback: check exception message for known patterns
    # (covers wrapped exceptions where the HTTP response isn't directly accessible)
    msg = str(exc).lower()
    return (
        "invalid_grant" in msg
        or "token has been revoked" in msg
        or "token has been expired or revoked" in msg
        or "interaction_required" in msg
    )


def _check_microsoft_error_codes(body: str) -> bool:
    """Check if Microsoft response body contains known permanent auth error codes.

    Microsoft embeds AADSTS codes both in error_description ("AADSTS70008: ...")
    and in the error_codes JSON array ([70008]). We check both patterns.
    """
    import re

    all_permanent_codes = _MS_PERMANENT_AUTH_CODES | _MS_INTERACTIVE_REAUTH_CODES

    # Check "AADSTS{code}" patterns in error_description
    aadsts_matches = re.findall(r"aadsts(\d+)", body)
    for code_str in aadsts_matches:
        try:
            if int(code_str) in all_permanent_codes:
                return True
        except ValueError:
            continue

    # Check error_codes array: "error_codes": [70008]
    code_array_matches = re.findall(r'"error_codes"\s*:\s*\[([^\]]+)\]', body)
    for codes_str in code_array_matches:
        for code_str in re.findall(r"\d+", codes_str):
            try:
                if int(code_str) in all_permanent_codes:
                    return True
            except ValueError:
                continue

    return False


def _compute_token_expiry(new_tokens: dict[str, Any]) -> datetime | None:
    """Compute token expiry datetime from provider response.

    expires_at = Unix epoch timestamp; expires_in = seconds from now.
    Google typically returns expires_in (3600), but some providers return
    expires_at — treating both as seconds would produce a wildly wrong expiry.
    """
    expires_at_raw = new_tokens.get("expires_at")
    expires_in_raw = new_tokens.get("expires_in")

    if expires_at_raw is not None and (
        isinstance(expires_at_raw, (int, float))
        or (isinstance(expires_at_raw, str) and expires_at_raw.isdigit())
    ):
        return datetime.fromtimestamp(int(expires_at_raw), tz=UTC).replace(tzinfo=None)
    if expires_in_raw is not None and (
        isinstance(expires_in_raw, (int, float))
        or (isinstance(expires_in_raw, str) and expires_in_raw.isdigit())
    ):
        return datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=int(expires_in_raw))
    return None


# Token refresh lock TTL — must exceed the longest possible refresh round-trip
# (network + provider latency + DB persist). 30s is generous; typical is <5s.
_TOKEN_REFRESH_LOCK_TTL = 30


async def _refresh_and_persist_token(token_info: IntegrationTokenInfo) -> str:
    """Refresh token via the appropriate provider and persist new credentials to DB.

    Called in two scenarios:
    - Proactively by get_access_token() when the token is expiring within 5 min
    - Reactively by call_with_token_retry() after a 401 from the provider

    Uses a Redis distributed lock keyed by integration_id to prevent concurrent
    refreshes (e.g., calendar + email syncing the same integration simultaneously).
    If another task is already refreshing, this waits briefly then reads the
    freshly persisted token from the DB instead of calling the provider again.

    Updates token_info in-place so subsequent pagination pages reuse the new token.
    """
    from sqlalchemy import select
    from src.domain.models.organisation_tables.sync import KnowledgeIntegration
    from src.infra.database.sessions import acquire_org_async_session
    from src.infra.utils.task_lock import acquire_task_lock, release_task_lock
    from src.service.integrations.providers.provider_factory import ProviderFactory

    if not token_info.integration_type:
        raise RuntimeError("integration_type required for token refresh")
    if not token_info.refresh_token:
        raise RuntimeError("refresh_token required for token refresh")
    if not token_info.organisation_id:
        raise RuntimeError("organisation_id required for token refresh")

    lock_key = f"token_refresh:{token_info.integration_id}"

    if not acquire_task_lock(lock_key, ttl=_TOKEN_REFRESH_LOCK_TTL):
        # Another task is refreshing this integration's token right now.
        # Wait briefly, then read the freshly persisted token from DB.
        import asyncio

        await asyncio.sleep(2)
        return await _read_fresh_token_from_db(token_info)

    try:
        provider = ProviderFactory.get_provider(token_info.integration_type)

        try:
            new_tokens = await provider.refresh_token(token_info.refresh_token)
        except Exception as exc:
            if _is_permanent_auth_error(exc):
                raise AuthTokenRevokedError(
                    f"Refresh token permanently invalid for integration "
                    f"{token_info.integration_id}: {exc}"
                ) from exc
            raise

        new_access_token = new_tokens.get("access_token")
        if not isinstance(new_access_token, str) or not new_access_token:
            raise RuntimeError("Provider refresh response missing access_token")

        new_expiry = _compute_token_expiry(new_tokens)

        async for session in acquire_org_async_session(token_info.organisation_id):
            stmt = select(KnowledgeIntegration).where(
                KnowledgeIntegration.integration_id == token_info.integration_id  # type: ignore[arg-type]
            )
            result = await session.execute(stmt)
            integration = result.scalar_one_or_none()
            if integration:
                credentials = dict(integration.credentials or {})
                credentials.update(new_tokens)
                # OAuth providers don't always return a new refresh_token —
                # preserve the existing one so we don't lose refresh capability
                if "refresh_token" not in new_tokens and token_info.refresh_token:
                    credentials["refresh_token"] = token_info.refresh_token
                integration.credentials = credentials

                if new_expiry is not None:
                    integration.access_token_expires_at = new_expiry

                session.add(integration)
                await session.commit()

        # Update in-memory so subsequent pagination pages in the same sync
        # use the new token without another DB round-trip
        token_info.access_token = new_access_token
        if "refresh_token" in new_tokens:
            token_info.refresh_token = new_tokens["refresh_token"]
        if new_expiry is not None:
            token_info.access_token_expires_at = new_expiry

        logger.info("Token refreshed inline: integration=%s", token_info.integration_id)
        return new_access_token
    finally:
        release_task_lock(lock_key)


async def _read_fresh_token_from_db(
    token_info: IntegrationTokenInfo,
) -> str:
    """Read a freshly refreshed token from DB after another task did the refresh.

    Called when the Redis lock indicates a concurrent refresh is in progress.
    Falls back to forcing a refresh if the DB token is still stale.
    """
    from sqlalchemy import select
    from src.domain.models.organisation_tables.sync import KnowledgeIntegration
    from src.infra.database.sessions import acquire_org_async_session

    async for session in acquire_org_async_session(token_info.organisation_id):  # type: ignore[arg-type]
        stmt = select(KnowledgeIntegration).where(
            KnowledgeIntegration.integration_id == token_info.integration_id  # type: ignore[arg-type]
        )
        result = await session.execute(stmt)
        integration = result.scalar_one_or_none()

        if integration:
            creds = integration.credentials or {}
            db_token = creds.get("access_token")
            if isinstance(db_token, str) and db_token and db_token != token_info.access_token:
                token_info.access_token = db_token
                if integration.access_token_expires_at:
                    token_info.access_token_expires_at = integration.access_token_expires_at
                logger.info(
                    "Read fresh token from DB (concurrent refresh): integration=%s",
                    token_info.integration_id,
                )
                return db_token

    # DB token unchanged — the other refresh may have failed. Force our own.
    logger.warning(
        "DB token unchanged after lock wait — forcing refresh: integration=%s",
        token_info.integration_id,
    )
    # Recursive call will acquire the lock (now released by the other task)
    return await _refresh_and_persist_token(token_info)


async def get_access_token(token_info: IntegrationTokenInfo) -> str:
    """Retrieve a fresh OAuth access token from local credentials.

    Refreshes proactively if token is expiring within 5 minutes.
    Called by all provider-specific fetch functions (Google Calendar,
    Gmail, and future Microsoft/CRM providers).
    """
    if _is_token_expiring_soon(token_info):
        return await _refresh_and_persist_token(token_info)
    if not token_info.access_token:
        raise RuntimeError(
            f"Custom OAuth integration {token_info.integration_id} has no access_token"
        )
    return token_info.access_token


async def call_with_token_retry(
    token_info: IntegrationTokenInfo,
    request_fn: Any,
) -> Any:
    """Execute request_fn with a token; on 401, refresh and retry once.

    Provider-agnostic — works for any API that returns HTTP 401 on expired tokens.
    """
    access_token = await get_access_token(token_info)
    try:
        return await request_fn(access_token)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            logger.warning(
                "Got 401, refreshing token and retrying: integration=%s",
                token_info.integration_id,
            )
            # _refresh_and_persist_token may raise AuthTokenRevokedError
            # if the refresh token is permanently invalid — let it propagate
            # so the caller can mark the integration as auth_failed
            new_token = await _refresh_and_persist_token(token_info)
            return await request_fn(new_token)
        raise
