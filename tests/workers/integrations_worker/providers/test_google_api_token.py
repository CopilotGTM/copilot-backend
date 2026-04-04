"""Tests for custom OAuth token resolution and 429 rate-limit retry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.workers.integrations_worker.providers._google_api import (
    IntegrationTokenInfo,
    _request_with_rate_limit_retry,
    call_with_token_retry,
    get_access_token,
)


@pytest.fixture
def valid_token_info():
    return IntegrationTokenInfo(
        access_token="valid-access-token-123",
        refresh_token="refresh-token-456",
        access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        integration_type=IntegrationType.GOOGLE_CALENDAR,
        organisation_id=uuid4(),
        integration_id=uuid4(),
    )


@pytest.fixture
def expired_token_info():
    return IntegrationTokenInfo(
        access_token="expired-access-token",
        refresh_token="refresh-token-456",
        access_token_expires_at=datetime.now(UTC) - timedelta(minutes=10),
        integration_type=IntegrationType.GOOGLE_CALENDAR,
        organisation_id=uuid4(),
        integration_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_get_access_token_returns_valid_token(valid_token_info):
    """Returns stored access_token when not expired."""
    token = await get_access_token(valid_token_info)
    assert token == "valid-access-token-123"


@pytest.mark.asyncio
async def test_get_access_token_refreshes_expired(expired_token_info):
    """Refreshes token when expired."""
    with patch(
        "src.service.integrations.providers.token_resolution._refresh_and_persist_token",
        new_callable=AsyncMock,
        return_value="refreshed-token-new",
    ) as mock_refresh:
        token = await get_access_token(expired_token_info)
        assert token == "refreshed-token-new"
        mock_refresh.assert_called_once_with(expired_token_info)


@pytest.mark.asyncio
async def test_get_access_token_refreshes_when_no_expiry(valid_token_info):
    """Refreshes when access_token_expires_at is None (unknown expiry)."""
    valid_token_info.access_token_expires_at = None
    with patch(
        "src.service.integrations.providers.token_resolution._refresh_and_persist_token",
        new_callable=AsyncMock,
        return_value="refreshed-token",
    ):
        token = await get_access_token(valid_token_info)
        assert token == "refreshed-token"


@pytest.mark.asyncio
async def test_get_access_token_raises_when_no_token(valid_token_info):
    """Raises RuntimeError when access_token is None and not expired."""
    valid_token_info.access_token = None
    with pytest.raises(RuntimeError, match="has no access_token"):
        await get_access_token(valid_token_info)


@pytest.mark.asyncio
async def test_call_with_token_retry_succeeds_first_try(valid_token_info):
    """Calls request_fn once when no 401."""
    mock_fn = AsyncMock(return_value={"items": []})
    result = await call_with_token_retry(valid_token_info, mock_fn)
    assert result == {"items": []}
    mock_fn.assert_called_once_with("valid-access-token-123")


@pytest.mark.asyncio
async def test_call_with_token_retry_refreshes_on_401(valid_token_info):
    """On 401, refreshes token and retries once."""
    import httpx

    mock_response = AsyncMock()
    mock_response.status_code = 401
    error_401 = httpx.HTTPStatusError("401", request=AsyncMock(), response=mock_response)

    call_count = 0

    async def mock_fn(token: str):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise error_401
        return {"items": ["retried"]}

    with patch(
        "src.service.integrations.providers.token_resolution._refresh_and_persist_token",
        new_callable=AsyncMock,
        return_value="refreshed-token",
    ):
        result = await call_with_token_retry(valid_token_info, mock_fn)
        assert result == {"items": ["retried"]}
        assert call_count == 2


# ---------------------------------------------------------------------------
# 429 Rate-limit retry tests
# ---------------------------------------------------------------------------


def _make_429_response(retry_after: str | None = None) -> httpx.Response:
    """Create a mock 429 response with optional Retry-After header."""
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    response = httpx.Response(
        status_code=429,
        headers=headers,
        request=httpx.Request("GET", "https://example.com"),
    )
    return response


def _make_429_error(retry_after: str | None = None) -> httpx.HTTPStatusError:
    """Create a 429 HTTPStatusError."""
    response = _make_429_response(retry_after)
    return httpx.HTTPStatusError(
        "429 Too Many Requests", request=response.request, response=response
    )


@pytest.mark.asyncio
async def test_rate_limit_retry_succeeds_first_try():
    """No retry needed when request succeeds immediately."""
    mock_fn = AsyncMock(return_value={"items": []})
    result = await _request_with_rate_limit_retry(mock_fn)
    assert result == {"items": []}
    mock_fn.assert_called_once()


@pytest.mark.asyncio
async def test_rate_limit_retry_recovers_on_429():
    """Retries once on 429 and succeeds on second attempt."""
    call_count = 0

    async def flaky_fn():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _make_429_error()
        return {"items": ["ok"]}

    with patch(
        "src.workers.integrations_worker.providers._google_api.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        result = await _request_with_rate_limit_retry(flaky_fn)

    assert result == {"items": ["ok"]}
    assert call_count == 2


@pytest.mark.asyncio
async def test_rate_limit_retry_respects_retry_after_header():
    """Uses Retry-After header value for backoff when present."""
    call_count = 0

    async def flaky_fn():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _make_429_error(retry_after="5")
        return {"items": []}

    with patch(
        "src.workers.integrations_worker.providers._google_api.asyncio.sleep",
        new_callable=AsyncMock,
    ) as mock_sleep:
        result = await _request_with_rate_limit_retry(flaky_fn)

    assert result == {"items": []}
    # Sleep should have been called with base of 5.0 + jitter (max 7.5)
    sleep_val = mock_sleep.call_args[0][0]
    assert 5.0 <= sleep_val <= 7.5


@pytest.mark.asyncio
async def test_rate_limit_retry_exhausts_retries():
    """Raises after max retries are exhausted."""

    async def always_429():
        raise _make_429_error()

    with (
        patch(
            "src.workers.integrations_worker.providers._google_api.asyncio.sleep",
            new_callable=AsyncMock,
        ),
        pytest.raises(httpx.HTTPStatusError) as exc_info,
    ):
        await _request_with_rate_limit_retry(always_429, max_retries=2)

    assert exc_info.value.response.status_code == 429


@pytest.mark.asyncio
async def test_rate_limit_retry_does_not_retry_non_429():
    """Non-429 errors (e.g. 500) are raised immediately without retry."""
    response_500 = httpx.Response(
        status_code=500,
        request=httpx.Request("GET", "https://example.com"),
    )
    error_500 = httpx.HTTPStatusError("500", request=response_500.request, response=response_500)

    async def server_error():
        raise error_500

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await _request_with_rate_limit_retry(server_error)

    assert exc_info.value.response.status_code == 500


@pytest.mark.asyncio
async def test_rate_limit_retry_exponential_backoff():
    """Backoff doubles on each retry attempt."""
    call_count = 0

    async def always_429():
        nonlocal call_count
        call_count += 1
        raise _make_429_error()

    sleep_values: list[float] = []

    async def capture_sleep(duration):
        sleep_values.append(duration)

    with (
        patch(
            "src.workers.integrations_worker.providers._google_api.asyncio.sleep",
            side_effect=capture_sleep,
        ),
        patch(
            "src.workers.integrations_worker.providers._google_api.random.uniform",
            return_value=0,  # Zero jitter for predictable assertions
        ),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await _request_with_rate_limit_retry(always_429, max_retries=3)

    # With zero jitter: attempt 0 → 1s, attempt 1 → 2s, attempt 2 → 4s
    assert len(sleep_values) == 3
    assert sleep_values[0] == 1.0
    assert sleep_values[1] == 2.0
    assert sleep_values[2] == 4.0
