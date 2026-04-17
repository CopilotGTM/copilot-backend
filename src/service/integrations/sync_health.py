"""Integration sync health tracking — error classification, circuit breaker, status persistence.

Centralizes sync outcome recording so both calendar and email org tasks
persist consistent status to KnowledgeIntegration. Called after every
integration sync attempt (success or failure).

Two separate mechanisms for two separate problems:

1. Auth failures (requires_reauth): User's refresh token is permanently
   invalid (revoked, expired, password change). Sets requires_reauth=True
   and is_active=False. Only cleared when user re-authenticates.

2. Circuit breaker (consecutive_sync_failures): Transient failures (network,
   provider outage, 500s) increment a counter. Org tasks skip integrations
   where counter >= CIRCUIT_BREAKER_THRESHOLD. Counter resets to 0 on the
   next success — so when the provider recovers, sync resumes automatically.
   Does NOT touch is_active — the integration remains "active" but skipped.
"""

from __future__ import annotations

from enum import Enum
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.api_error_utils import (
    IntegrationPermissionError,
    is_insufficient_scope_error,
)

logger = get_logger(__name__)

# Org tasks skip integrations with this many consecutive transient failures.
# The counter resets on the next success, so recovery is automatic.
CIRCUIT_BREAKER_THRESHOLD = 5


class SyncFailureType(str, Enum):
    """Classification of sync failure for observability and circuit breaker routing."""

    AUTH_FAILED = "auth_failed"
    RATE_LIMITED = "rate_limited"
    PROVIDER_ERROR = "provider_error"
    INTERNAL_ERROR = "internal_error"


class AuthTokenRevokedError(Exception):
    """Raised when a token refresh fails with a permanent auth error.

    Google returns 'invalid_grant' when the user revokes access from their
    Google Account, or when the refresh token expires (6-month inactivity).
    Microsoft returns AADSTS codes (50173, 70008, etc.) for similar scenarios.

    Non-recoverable — the user must re-authenticate. Caught by org tasks
    to set requires_reauth=True immediately.
    """

    pass


def classify_sync_error(exc: Exception) -> SyncFailureType:
    """Classify an exception into a failure type for observability and routing.

    Used by org tasks to determine whether a failure is permanent (auth),
    transient (rate limit, provider error), or internal (our bug).
    """
    if isinstance(exc, AuthTokenRevokedError):
        return SyncFailureType.AUTH_FAILED
    if isinstance(exc, IntegrationPermissionError):
        return SyncFailureType.AUTH_FAILED

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 401:
            return SyncFailureType.AUTH_FAILED
        if status == 403 and is_insufficient_scope_error(exc):
            return SyncFailureType.AUTH_FAILED
        if status == 403:
            return SyncFailureType.PROVIDER_ERROR
        if status == 429:
            return SyncFailureType.RATE_LIMITED
        if 500 <= status < 600:
            return SyncFailureType.PROVIDER_ERROR

    msg = str(exc).lower()
    if (
        "invalid_grant" in msg
        or "token has been revoked" in msg
        or is_insufficient_scope_error(exc)
    ):
        return SyncFailureType.AUTH_FAILED

    if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)):
        return SyncFailureType.PROVIDER_ERROR

    return SyncFailureType.INTERNAL_ERROR


def apply_sync_success(integration: KnowledgeIntegration) -> None:
    """Apply sync success fields to an already-loaded integration instance.

    Use this when the caller already holds a session (e.g., calendar org_task
    persist phase) to avoid a second DB round-trip. The caller is responsible
    for committing the session.

    Does NOT clear requires_reauth — only the re-auth flow does that.
    """
    integration.last_sync_status = "success"
    integration.last_sync_error = None
    integration.consecutive_sync_failures = 0


async def record_sync_success(
    *,
    organisation_id: UUID,
    integration_id: UUID,
) -> None:
    """Record a successful sync — reset failure counter and update status.

    Called by org tasks that don't hold their own session (e.g., email org_task
    delegates to EmailDeltaSyncService). For callers that already hold a session,
    prefer apply_sync_success() to avoid a second DB round-trip.

    Does NOT clear requires_reauth — that flag is only cleared when the
    user explicitly re-authenticates via the OAuth flow.
    """
    async for session in acquire_org_async_session(organisation_id):
        stmt = select(KnowledgeIntegration).where(
            col(KnowledgeIntegration.integration_id) == integration_id,
        )
        result = await session.execute(stmt)
        integration = result.scalar_one_or_none()

        if not integration:
            return

        apply_sync_success(integration)
        session.add(integration)
        await session.commit()

    logger.info(
        "Sync success recorded: integration=%s org=%s",
        integration_id,
        organisation_id,
    )


async def record_sync_failure(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    error: Exception,
    failure_type: SyncFailureType | None = None,
) -> SyncFailureType:
    """Record a failed sync — increment counter, possibly flag for re-auth.

    Returns the classified failure type for the caller's result dict.

    Auth failures: set requires_reauth=True and is_active=False immediately.
    The user must re-authenticate — no amount of retrying will fix this.

    Transient failures: increment consecutive_sync_failures only. Do NOT
    touch is_active. Org tasks skip integrations where the counter exceeds
    CIRCUIT_BREAKER_THRESHOLD, but the counter resets to 0 on the next
    success — so recovery is fully automatic.
    """
    if failure_type is None:
        failure_type = classify_sync_error(error)

    error_msg = str(error)[:500]

    new_count = 0

    async for session in acquire_org_async_session(organisation_id):
        stmt = select(KnowledgeIntegration).where(
            col(KnowledgeIntegration.integration_id) == integration_id,
        )
        result = await session.execute(stmt)
        integration = result.scalar_one_or_none()

        if not integration:
            return failure_type

        integration.last_sync_status = failure_type.value
        integration.last_sync_error = error_msg
        new_count = (integration.consecutive_sync_failures or 0) + 1
        integration.consecutive_sync_failures = new_count

        if failure_type == SyncFailureType.AUTH_FAILED:
            # Permanent auth failure — flag for re-auth and deactivate
            integration.requires_reauth = True
            integration.is_active = False
            logger.warning(
                "Auth failure — requires_reauth set, integration deactivated: "
                "integration=%s org=%s error=%s",
                integration_id,
                organisation_id,
                error_msg[:200],
            )
        elif new_count >= CIRCUIT_BREAKER_THRESHOLD:
            # Circuit breaker threshold reached — log but do NOT deactivate.
            # Org tasks will skip this integration based on the counter.
            # When the provider recovers, the next scheduled sync attempt
            # that slips through will succeed and reset the counter.
            logger.warning(
                "Circuit breaker threshold reached (%d consecutive failures), "
                "sync will be skipped until recovery: integration=%s org=%s",
                new_count,
                integration_id,
                organisation_id,
            )

        session.add(integration)
        await session.commit()

    logger.info(
        "Sync failure recorded: integration=%s org=%s type=%s consecutive_failures=%d",
        integration_id,
        organisation_id,
        failure_type.value,
        new_count,
    )
    return failure_type
