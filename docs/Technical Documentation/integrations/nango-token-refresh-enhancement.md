# Nango Token Refresh Enhancement Plan

## Implementation Decisions
- **Scope:** Full implementation (Phases 1-5)
- **Deactivation threshold:** 3 consecutive failures (recommended balance)

---

## Problem Summary

Nango integration tokens are expiring, causing services to fail and run until they hit soft limits. The current token refresh mechanism exists but is **disabled**, and there's no graceful error handling for token failures during API calls.

## Understanding Token Types and Expiration

There are **two types of token expiration** that need different handling:

### 1. Access Token Expiration (Handled Automatically by Nango Proxy)

**What it is:** Access tokens are short-lived (typically 1-2 hours) credentials used to make API calls.

**How Nango handles it:**
- When you make a request via Nango proxy and the access token is expired (or will expire within 15 minutes)
- Nango automatically uses the **refresh token** to get a new access token
- Nango retries the request with the new token
- **This is transparent to your code** - no action needed

**Our backup (Phase 2):** We add retry logic in case Nango's auto-refresh fails for any reason.

### 2. Refresh Token Expiration/Revocation (CANNOT be auto-fixed)

**What it is:** Refresh tokens are long-lived credentials used to obtain new access tokens.

**Why they expire or get revoked:**
- **Inactivity:** Google revokes refresh tokens unused for 6 months. HubSpot and others have similar policies.
- **User revocation:** User disconnects the app from their account settings
- **Policy changes:** Provider invalidates tokens due to security or policy updates
- **Token limits:** Some providers (HubSpot) limit active refresh tokens per app

**Why Nango CANNOT fix this:** When refresh token is invalid, the only solution is **user re-authentication**.

**How our scheduled task helps:**
1. **Prevents inactivity expiration:** By "touching" the token every 30 minutes, we keep it active
2. **Early detection:** When refresh fails, we detect it before users experience failures
3. **Graceful degradation:** We mark integrations needing re-auth after 3 consecutive failures

### Summary Table

| Token Type | Lifespan | Auto-Fixable? | How We Handle |
|------------|----------|---------------|---------------|
| **Access Token** | 1-2 hours | **Yes** - Nango proxy refreshes automatically | Backup retry logic (Phase 2) |
| **Refresh Token** | Weeks to months | **No** - User must re-authenticate | Scheduled task prevents expiry + detects revocation (Phase 1, 3) |

### Why Run the 30-Minute Scheduled Task?

Even though Nango proxy handles access token refresh automatically, the scheduled task serves critical purposes:

1. **Prevents refresh token expiration:** Keeps tokens "warm" by using them regularly
   - From [Nango docs](https://nango.dev/blog/salesforce-oauth-refresh-token-invalid-grant): "Set up a cron job to refresh all tokens, e.g. daily"
   - We do it every 30 minutes to be safe

2. **Early problem detection:** Discovers broken integrations before users experience failures

3. **Database sync:** Keeps our `access_token_expires_at` in sync with Nango's actual state

---

## Key Insight from Research

**Nango proxy already handles access token refresh automatically** - when a request fails with 401, Nango refreshes the token and retries. The main issues in our codebase are:

1. Proactive refresh task is disabled (commented out in config) - **refresh tokens could expire from inactivity**
2. No application-level retry for transient failures
3. Immediate integration deactivation on first failure (too aggressive)
4. No proper error classification (401 vs other errors)

### Sources
- [Nango OAuth Concurrency Best Practices](https://nango.dev/blog/concurrency-with-oauth-token-refreshes)
- [Nango API Auth Guide](https://nango.dev/docs/guides/use-cases/api-auth)
- [Nango Node SDK Reference](https://nango.dev/docs/reference/sdks/node)

---

## Implementation Plan

### Phase 1: Enable Proactive Token Refresh (Critical)

**File:** `src/workers/integrations_worker/config.py`

**Current State:** The `maintenance_schedule` is commented out and not included in `beat_schedule`.

**Changes:**
1. Uncomment the `maintenance_schedule` section
2. Change schedule from hourly to every 30 minutes: `crontab(minute="0,30")`
3. Include `maintenance_schedule` in the `beat_schedule` merge

**Code Changes:**

```python
# Current (disabled):
# maintenance_schedule = {
#     "refresh-expiring-oauth-tokens": {
#         "task": "integrations.refresh_expiring_tokens",
#         "schedule": crontab(minute=0),  # Hourly token refresh window
#         "kwargs": {"buffer_hours": 2},
#         "options": {"queue": "integrations", "routing_key": "integrations"},
#     },
# }

# Proposed:
maintenance_schedule = {
    "refresh-expiring-oauth-tokens": {
        "task": "integrations.refresh_expiring_tokens",
        "schedule": crontab(minute="0,30"),  # Every 30 minutes
        "kwargs": {"buffer_hours": 2},
        "options": {"queue": "integrations", "routing_key": "integrations"},
    },
}

# And in beat_schedule initialization:
self.beat_schedule = {
    **crm_schedule,
    **maintenance_schedule,  # <-- Add this line
    **communication_schedule,
}
```

---

### Phase 2: Add Retry Logic with 401 Detection (Critical)

**File:** `src/service/integrations/nango_client.py`

**Changes:**

#### 2.1 Add new exception class (after line 46):

```python
class NangoAuthError(NangoConnectionError):
    """Raised when authentication/authorization fails (401/403)."""
    pass
```

#### 2.2 Enhance `proxy_request()` method (replace lines 706-802):

Add retry logic with:
- Automatic retry with exponential backoff for transient failures (5xx, timeouts)
- Specific 401/403 detection that triggers token refresh attempt
- Rate limit (429) handling with Retry-After header respect
- Max 3 retries by default

**New method signature:**

```python
async def proxy_request(
    self,
    provider_config_key: str,
    connection_id: str,
    endpoint: str,
    method: str = "GET",
    data: dict | None = None,
    params: dict | None = None,
    max_retries: int = 3,
    retry_on_auth_error: bool = True,
) -> dict[str, Any]:
```

**Retry logic:**

```python
import random

for attempt in range(max_retries + 1):
    try:
        return await self._execute_proxy_request(...)
    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code

        # Handle 401/403 - try token refresh on first attempt
        if status_code in (401, 403):
            if retry_on_auth_error and attempt == 0:
                try:
                    await self.refresh_token(provider_config_key, connection_id)
                    continue  # Retry after refresh
                except Exception:
                    pass
            raise NangoAuthError(...) from None

        # Handle 429 rate limiting
        if status_code == 429:
            retry_after = int(e.response.headers.get("Retry-After", 60))
            if attempt < max_retries:
                await asyncio.sleep(min(retry_after, 120))
                continue

        # Handle 5xx server errors with backoff
        if status_code >= 500 and attempt < max_retries:
            backoff = (2 ** attempt) + random.random()
            await asyncio.sleep(backoff)
            continue

        raise NangoConnectionError(...) from None
```

#### 2.3 Extract execution logic to helper method:

```python
async def _execute_proxy_request(
    self,
    provider_config_key: str,
    connection_id: str,
    endpoint: str,
    method: str,
    data: dict | None,
    params: dict | None,
) -> dict[str, Any]:
    """Execute a single proxy request without retry logic."""
    # Move existing request execution code here
```

**File:** `src/service/integrations/__init__.py`

Add export:
```python
from .nango_client import NangoClient, NangoConnectionError, NangoTokenError, NangoAuthError
```

---

### Phase 3: Implement Escalating Failure Handling (High)

**File:** `src/service/integrations/token_refresh_service.py`

**Current Issue:** `_handle_refresh_failure()` deactivates integration immediately on first failure.

**Changes:**

#### 3.1 Modify `_handle_refresh_failure()` (replace lines 227-260):

```python
def _handle_refresh_failure(self, integration: KnowledgeIntegration, error_message: str) -> int:
    """Handle token refresh failure with escalating retry instead of immediate deactivation."""
    try:
        # Track failure count in connection_metadata (no migration needed)
        metadata = integration.connection_metadata or {}
        failure_count = metadata.get("token_refresh_failures", 0) + 1

        metadata["token_refresh_failures"] = failure_count
        metadata["last_refresh_failure_at"] = datetime.now(UTC).isoformat()
        metadata["last_refresh_error"] = error_message[:200]
        integration.connection_metadata = metadata

        MAX_CONSECUTIVE_FAILURES = 3  # Deactivate after 3 consecutive failures

        if failure_count >= MAX_CONSECUTIVE_FAILURES:
            # Deactivate only after consecutive failures
            integration.is_active = False
            integration.last_sync_status = "FAILED"
            integration.last_sync_error = f"Token refresh failed {failure_count} times: {error_message}"
            integration.updated_at = datetime.now(UTC)
            self.db.commit()

            logger.warning(
                "Deactivated integration %s after %d consecutive token refresh failures",
                integration.integration_id,
                failure_count,
            )
            self._log_refresh_failure(integration)
            return 1
        else:
            # Log warning but keep active - will retry next scheduled run
            integration.updated_at = datetime.now(UTC)
            self.db.commit()

            logger.warning(
                "Token refresh failed for integration %s (attempt %d/%d): %s",
                integration.integration_id,
                failure_count,
                MAX_CONSECUTIVE_FAILURES,
                error_message,
            )
            return 0

    except Exception as e:
        logger.error(
            "Failed to handle refresh failure for integration %s: %s",
            integration.integration_id,
            str(e),
            exc_info=True,
        )
        return 0
```

#### 3.2 Reset failure count on success in `refresh_integration_token()`:

Add after successful refresh (around line 128):

```python
# Reset failure counter on success
metadata = integration.connection_metadata or {}
if metadata.get("token_refresh_failures", 0) > 0:
    metadata["token_refresh_failures"] = 0
    metadata.pop("last_refresh_failure_at", None)
    metadata.pop("last_refresh_error", None)
    integration.connection_metadata = metadata
```

---

### Phase 4: Add Error Handling to BaseFetcher (High)

**File:** `src/workers/integrations_worker/core/base_fetcher.py`

**Current Issue:** No try/except in `_fetch_paginated()` and `_fetch_batch()` - exceptions crash pagination loops.

**Changes:**

#### 4.1 Add imports and exception class (at top):

```python
from src.service.integrations.nango_client import NangoConnectionError, NangoAuthError

class TokenExpiredError(Exception):
    """Raised when token is expired and refresh failed."""
    pass
```

#### 4.2 Add error handling to `_fetch_paginated()` (lines 114-120):

```python
try:
    response = await self.nango_client.proxy_request(
        provider_config_key=self.provider_config_key,
        connection_id=self.connection_id,
        endpoint=endpoint,
        method="GET",
        params=params,
    )
except NangoAuthError as e:
    logger.error(
        "Authentication failed during pagination (page %d): %s",
        page,
        str(e),
    )
    raise TokenExpiredError(
        f"Token expired or revoked for connection {self.connection_id}"
    ) from e
except NangoConnectionError as e:
    logger.error(
        "Connection error during pagination (page %d, %d records so far): %s",
        page,
        len(all_records),
        str(e),
    )
    raise
```

#### 4.3 Add similar error handling to `_fetch_batch()` (lines 180-186):

Same pattern as above.

---

### Phase 5: Improve Error Detection in Services (Medium)

**File:** `src/service/integrations/transcript_service.py`

**Current Issue:** Uses string-based error detection (`"auth" in error_msg`).

**Changes:**

Replace generic exception handling with specific exception types:

```python
from src.service.integrations.nango_client import NangoAuthError, NangoConnectionError

# In _try_fetch_teams_with_integration() and similar methods:

try:
    # ... existing fetch code ...
except NangoAuthError as e:
    logger.warning(
        "Auth error fetching transcript with integration %s: %s",
        integration.integration_email,
        str(e),
    )
    return TranscriptFetchResult(
        success=False,
        meeting=meeting,
        error=f"Authentication failed: {str(e)[:300]}",
        should_retry_other_integration=True,
    )
except NangoConnectionError as e:
    logger.warning(
        "Connection error fetching transcript with integration %s: %s",
        integration.integration_email,
        str(e),
    )
    return TranscriptFetchResult(
        success=False,
        meeting=meeting,
        error=str(e)[:500],
        should_retry_other_integration=True,
    )
except Exception as e:
    logger.error(
        "Unexpected error fetching transcript: %s",
        str(e),
        exc_info=True,
    )
    return TranscriptFetchResult(
        success=False,
        meeting=meeting,
        error=str(e)[:500],
        should_retry_other_integration=False,
    )
```

---

## Files to Modify Summary

| Priority | File | Changes |
|----------|------|---------|
| Critical | `src/workers/integrations_worker/config.py` | Enable maintenance_schedule, add to beat_schedule |
| Critical | `src/service/integrations/nango_client.py` | Add NangoAuthError, retry logic, _execute_proxy_request |
| Critical | `src/service/integrations/__init__.py` | Export NangoAuthError |
| High | `src/service/integrations/token_refresh_service.py` | Escalating failure handling |
| High | `src/workers/integrations_worker/core/base_fetcher.py` | Error handling in pagination |
| Medium | `src/service/integrations/transcript_service.py` | Exception-based error detection |

---

## Verification Plan

1. **Type checking:** Run `pyright` to ensure no type errors introduced
2. **Unit tests:** Test proxy_request retry logic with mocked 401/429/500 responses
3. **Integration test:** Verify token refresh task runs on schedule
4. **Manual test:** Force token expiration and verify graceful handling
5. **Monitoring:** Check logs for "Auth error" and "Token refresh" messages

---

## Out of Scope (Future Enhancements)

1. Webhook support for credential failure notifications from Nango
2. Database sync of token expiry after Nango auto-refresh via proxy
3. Circuit breaker pattern for repeated failures
4. User notification (email/in-app) on integration deactivation
5. Configuration options in `configs.py` for thresholds

---

## Risk Assessment

| Risk | Mitigation |
|------|------------|
| Retry logic increases latency | Exponential backoff + max 3 retries keeps it bounded |
| Rate limit violation on Nango | Respect Retry-After header, cap at 2 minutes |
| Too many token refresh calls | Nango handles this internally, only one refresh per failure |
| Breaking existing behavior | Gradual rollout, monitor logs for unexpected errors |
