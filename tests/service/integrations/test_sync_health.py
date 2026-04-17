"""Tests for sync health module — error classification, circuit breaker, auth revocation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from src.service.integrations.sync_health import (
    CIRCUIT_BREAKER_THRESHOLD,
    AuthTokenRevokedError,
    SyncFailureType,
    classify_sync_error,
    record_sync_failure,
    record_sync_success,
)

# ---------------------------------------------------------------------------
# classify_sync_error tests
# ---------------------------------------------------------------------------


class TestClassifySyncError:
    def test_auth_token_revoked_error(self):
        exc = AuthTokenRevokedError("token revoked")
        assert classify_sync_error(exc) == SyncFailureType.AUTH_FAILED

    def test_http_401(self):
        response = httpx.Response(401, request=httpx.Request("GET", "https://x.com"))
        exc = httpx.HTTPStatusError("401", request=response.request, response=response)
        assert classify_sync_error(exc) == SyncFailureType.AUTH_FAILED

    def test_http_403(self):
        response = httpx.Response(403, request=httpx.Request("GET", "https://x.com"))
        exc = httpx.HTTPStatusError("403", request=response.request, response=response)
        assert classify_sync_error(exc) == SyncFailureType.PROVIDER_ERROR

    def test_http_403_insufficient_scope(self):
        response = httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "message": "Request had insufficient authentication scopes.",
                    "status": "PERMISSION_DENIED",
                }
            },
            request=httpx.Request("GET", "https://x.com"),
        )
        exc = httpx.HTTPStatusError("403", request=response.request, response=response)
        assert classify_sync_error(exc) == SyncFailureType.AUTH_FAILED

    def test_http_429(self):
        response = httpx.Response(429, request=httpx.Request("GET", "https://x.com"))
        exc = httpx.HTTPStatusError("429", request=response.request, response=response)
        assert classify_sync_error(exc) == SyncFailureType.RATE_LIMITED

    def test_http_500(self):
        response = httpx.Response(500, request=httpx.Request("GET", "https://x.com"))
        exc = httpx.HTTPStatusError("500", request=response.request, response=response)
        assert classify_sync_error(exc) == SyncFailureType.PROVIDER_ERROR

    def test_http_502(self):
        response = httpx.Response(502, request=httpx.Request("GET", "https://x.com"))
        exc = httpx.HTTPStatusError("502", request=response.request, response=response)
        assert classify_sync_error(exc) == SyncFailureType.PROVIDER_ERROR

    def test_invalid_grant_in_message(self):
        exc = Exception("Google error: invalid_grant")
        assert classify_sync_error(exc) == SyncFailureType.AUTH_FAILED

    def test_token_revoked_in_message(self):
        exc = Exception("Token has been revoked by the user")
        assert classify_sync_error(exc) == SyncFailureType.AUTH_FAILED

    def test_missing_scope_in_message(self):
        exc = Exception("Slack conversations.history failed: missing_scope")
        assert classify_sync_error(exc) == SyncFailureType.AUTH_FAILED

    def test_connect_error(self):
        exc = httpx.ConnectError("Connection refused")
        assert classify_sync_error(exc) == SyncFailureType.PROVIDER_ERROR

    def test_timeout_error(self):
        exc = httpx.ReadTimeout("Read timed out")
        assert classify_sync_error(exc) == SyncFailureType.PROVIDER_ERROR

    def test_generic_exception(self):
        exc = ValueError("some internal bug")
        assert classify_sync_error(exc) == SyncFailureType.INTERNAL_ERROR


# ---------------------------------------------------------------------------
# Helpers for mocking DB session
# ---------------------------------------------------------------------------


def _make_mock_integration(
    consecutive_failures: int = 0,
    last_sync_status: str | None = None,
    is_active: bool = True,
    requires_reauth: bool = False,
):
    """Create a mock KnowledgeIntegration with settable fields."""
    integration = MagicMock()
    integration.consecutive_sync_failures = consecutive_failures
    integration.last_sync_status = last_sync_status
    integration.last_sync_error = "some old error"
    integration.is_active = is_active
    integration.requires_reauth = requires_reauth
    return integration


def _mock_session_with_integration(integration):
    """Create a mock session that returns the given integration."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = integration
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    async def mock_acquire(*args, **kwargs):
        yield mock_session

    return mock_session, mock_acquire


# ---------------------------------------------------------------------------
# record_sync_success tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_sync_success_resets_counter():
    """Success resets consecutive_sync_failures to 0 and clears error."""
    mock_integration = _make_mock_integration(
        consecutive_failures=3, last_sync_status="provider_error"
    )
    mock_session, mock_acquire = _mock_session_with_integration(mock_integration)

    with patch(
        "src.service.integrations.sync_health.acquire_org_async_session",
        side_effect=mock_acquire,
    ):
        await record_sync_success(organisation_id=uuid4(), integration_id=uuid4())

    assert mock_integration.last_sync_status == "success"
    assert mock_integration.last_sync_error is None
    assert mock_integration.consecutive_sync_failures == 0
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_record_sync_success_does_not_clear_requires_reauth():
    """Success does NOT clear requires_reauth — only re-auth flow does that."""
    mock_integration = _make_mock_integration(requires_reauth=True, is_active=False)
    mock_session, mock_acquire = _mock_session_with_integration(mock_integration)

    with patch(
        "src.service.integrations.sync_health.acquire_org_async_session",
        side_effect=mock_acquire,
    ):
        await record_sync_success(organisation_id=uuid4(), integration_id=uuid4())

    # requires_reauth should still be True
    assert mock_integration.requires_reauth is True


# ---------------------------------------------------------------------------
# record_sync_failure tests — transient failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_sync_failure_increments_counter():
    """Transient failure increments counter but does NOT touch is_active."""
    mock_integration = _make_mock_integration(consecutive_failures=2)
    _, mock_acquire = _mock_session_with_integration(mock_integration)

    with patch(
        "src.service.integrations.sync_health.acquire_org_async_session",
        side_effect=mock_acquire,
    ):
        failure_type = await record_sync_failure(
            organisation_id=uuid4(),
            integration_id=uuid4(),
            error=httpx.ReadTimeout("timeout"),
        )

    assert failure_type == SyncFailureType.PROVIDER_ERROR
    assert mock_integration.consecutive_sync_failures == 3
    assert mock_integration.is_active is True  # NOT deactivated


@pytest.mark.asyncio
async def test_record_sync_failure_circuit_breaker_does_not_deactivate():
    """At threshold, counter increments but is_active stays True.

    Org tasks skip by counter, not by is_active — so recovery is automatic.
    """
    mock_integration = _make_mock_integration(
        consecutive_failures=CIRCUIT_BREAKER_THRESHOLD - 1,
    )
    _, mock_acquire = _mock_session_with_integration(mock_integration)

    with patch(
        "src.service.integrations.sync_health.acquire_org_async_session",
        side_effect=mock_acquire,
    ):
        await record_sync_failure(
            organisation_id=uuid4(),
            integration_id=uuid4(),
            error=httpx.ConnectError("connection refused"),
        )

    assert mock_integration.consecutive_sync_failures == CIRCUIT_BREAKER_THRESHOLD
    assert mock_integration.is_active is True  # Still active — just skipped by counter


# ---------------------------------------------------------------------------
# record_sync_failure tests — auth failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_sync_failure_auth_sets_requires_reauth():
    """Auth failure sets requires_reauth=True and is_active=False immediately."""
    mock_integration = _make_mock_integration(consecutive_failures=0)
    _, mock_acquire = _mock_session_with_integration(mock_integration)

    with patch(
        "src.service.integrations.sync_health.acquire_org_async_session",
        side_effect=mock_acquire,
    ):
        failure_type = await record_sync_failure(
            organisation_id=uuid4(),
            integration_id=uuid4(),
            error=AuthTokenRevokedError("invalid_grant"),
        )

    assert failure_type == SyncFailureType.AUTH_FAILED
    assert mock_integration.requires_reauth is True
    assert mock_integration.is_active is False
    assert mock_integration.last_sync_status == "auth_failed"


# ---------------------------------------------------------------------------
# _is_permanent_auth_error tests — comprehensive Google/Microsoft detection
# ---------------------------------------------------------------------------


class TestIsPermanentAuthError:
    """Tests for token_resolution._is_permanent_auth_error."""

    @pytest.fixture(autouse=True)
    def _import_fn(self):
        from src.service.integrations.providers.token_resolution import (
            _is_permanent_auth_error,
        )

        self.fn = _is_permanent_auth_error

    # -- Google errors --

    def test_google_invalid_grant(self):
        """Google 400 with invalid_grant (most common revocation signal)."""
        response = httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "Token has been expired or revoked.",
            },
            request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
        )
        exc = httpx.HTTPStatusError("400", request=response.request, response=response)
        assert self.fn(exc) is True

    def test_google_invalid_grant_no_description(self):
        """Google may omit error_description ~12h after revocation."""
        response = httpx.Response(
            400,
            json={"error": "invalid_grant"},
            request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
        )
        exc = httpx.HTTPStatusError("400", request=response.request, response=response)
        assert self.fn(exc) is True

    def test_google_admin_policy_enforced(self):
        """Google Workspace admin restricted app scopes."""
        response = httpx.Response(
            400,
            json={"error": "admin_policy_enforced"},
            request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
        )
        exc = httpx.HTTPStatusError("400", request=response.request, response=response)
        assert self.fn(exc) is True

    def test_google_invalid_client(self):
        """Client secret rotated — credentials no longer valid."""
        response = httpx.Response(
            400,
            json={
                "error": "invalid_client",
                "error_description": "The OAuth client was not found.",
            },
            request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
        )
        exc = httpx.HTTPStatusError("400", request=response.request, response=response)
        assert self.fn(exc) is True

    # -- Microsoft errors --

    def test_microsoft_aadsts70008_inactivity(self):
        """Microsoft refresh token expired due to inactivity."""
        response = httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "AADSTS70008: The refresh token has expired due to inactivity.",
                "error_codes": [70008],
            },
            request=httpx.Request("POST", "https://login.microsoftonline.com/token"),
        )
        exc = httpx.HTTPStatusError("400", request=response.request, response=response)
        assert self.fn(exc) is True

    def test_microsoft_aadsts50173_password_change(self):
        """Microsoft token revoked due to password change."""
        response = httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "AADSTS50173: The provided grant has expired due to it being revoked.",
                "error_codes": [50173],
            },
            request=httpx.Request("POST", "https://login.microsoftonline.com/token"),
        )
        exc = httpx.HTTPStatusError("400", request=response.request, response=response)
        assert self.fn(exc) is True

    def test_microsoft_aadsts65001_consent_revoked(self):
        """Microsoft admin/user revoked consent."""
        response = httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "AADSTS65001: The user or administrator has not consented.",
                "error_codes": [65001],
            },
            request=httpx.Request("POST", "https://login.microsoftonline.com/token"),
        )
        exc = httpx.HTTPStatusError("400", request=response.request, response=response)
        assert self.fn(exc) is True

    def test_microsoft_interaction_required(self):
        """Microsoft requires interactive re-auth (MFA/Conditional Access)."""
        response = httpx.Response(
            400,
            json={
                "error": "interaction_required",
                "error_description": "AADSTS50076: Due to a configuration change, MFA is required.",
                "error_codes": [50076],
            },
            request=httpx.Request("POST", "https://login.microsoftonline.com/token"),
        )
        exc = httpx.HTTPStatusError("400", request=response.request, response=response)
        assert self.fn(exc) is True

    # -- Negative cases --

    def test_non_400_status_not_detected(self):
        """500 errors are provider errors, not auth failures."""
        response = httpx.Response(
            500,
            text="Internal Server Error",
            request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
        )
        exc = httpx.HTTPStatusError("500", request=response.request, response=response)
        assert self.fn(exc) is False

    def test_generic_400_not_auth_error(self):
        """400 without auth-related error fields is not a permanent auth error."""
        response = httpx.Response(
            400,
            json={"error": "invalid_request", "error_description": "Missing required parameter."},
            request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
        )
        exc = httpx.HTTPStatusError("400", request=response.request, response=response)
        assert self.fn(exc) is False

    def test_timeout_not_auth_error(self):
        """Timeouts are transient, not auth failures."""
        exc = httpx.ReadTimeout("Read timed out")
        assert self.fn(exc) is False

    # -- Fallback message detection --

    def test_fallback_invalid_grant_in_message(self):
        """Wrapped exception with invalid_grant in message string."""
        exc = Exception("Failed to refresh: invalid_grant from provider")
        assert self.fn(exc) is True

    def test_fallback_token_revoked_in_message(self):
        exc = Exception("Token has been revoked by user")
        assert self.fn(exc) is True

    def test_fallback_interaction_required_in_message(self):
        exc = Exception("Error: interaction_required - MFA needed")
        assert self.fn(exc) is True


# ---------------------------------------------------------------------------
# AuthTokenRevokedError raised by _refresh_and_persist_token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_refresh_raises_auth_revoked_on_invalid_grant():
    """_refresh_and_persist_token raises AuthTokenRevokedError on invalid_grant."""
    from src.service.integrations.providers.token_resolution import (
        IntegrationTokenInfo,
        _refresh_and_persist_token,
    )

    token_info = IntegrationTokenInfo(
        access_token="old-token",
        refresh_token="revoked-refresh-token",
        integration_type="google_calendar",
        organisation_id=uuid4(),
        integration_id=uuid4(),
    )

    response = httpx.Response(
        400,
        json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
        request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
    )
    error = httpx.HTTPStatusError("400 Bad Request", request=response.request, response=response)

    mock_provider = AsyncMock()
    mock_provider.refresh_token = AsyncMock(side_effect=error)

    with patch(
        "src.service.integrations.providers.provider_factory.ProviderFactory"
    ) as mock_factory:
        mock_factory.get_provider.return_value = mock_provider

        with pytest.raises(AuthTokenRevokedError, match="permanently invalid"):
            await _refresh_and_persist_token(token_info)
