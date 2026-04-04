# Error Handling and Resilience

## Error Categories

Errors are classified by `classify_sync_error()` in `src/service/integrations/sync_health.py`:

| Type | Examples | Action |
|------|----------|--------|
| `AUTH_FAILED` | 401, 403, `AuthTokenRevokedError`, `invalid_grant` | Set `requires_reauth=True`, deactivate integration |
| `RATE_LIMITED` | 429 | Retry with backoff (handled inline) |
| `PROVIDER_ERROR` | 5xx, network timeout, DNS failure | Increment failure counter |
| `INTERNAL_ERROR` | Bug in our code | Increment failure counter |

## 401 Handling (Token Refresh on Auth Failure)

All provider fetchers wrap API calls in `call_with_token_retry()`:

```
1. get_access_token() — proactively refreshes if expiring within 5 minutes
2. Execute API call with token
3. If 401: refresh token, retry once
4. If refresh fails with permanent error: raise AuthTokenRevokedError
```

**Google**: Gmail and Google Calendar use `call_with_token_retry` via `_google_api.py`.

**Microsoft**: Outlook Email, Outlook Calendar, Teams (all 4 fetch functions) use `call_with_token_retry` via `_microsoft_graph.py`.

## 429 Rate Limit Retry

Both Google and Microsoft API calls go through provider-specific rate limit wrappers:

**Google** (`_google_api.py` → `_request_with_rate_limit_retry`):
- Max retries: 3
- Exponential backoff: 1s base, 30s max
- Jitter: random 0 to 50% of wait time
- Respects `Retry-After` header

**Microsoft** (`_microsoft_graph.py` → `_request_with_rate_limit_retry`):
- Same parameters as Google
- Applied to `graph_api_request()` and `graph_api_request_url()`

Both wrappers only retry on 429. All other HTTP errors propagate immediately.

## 410 Gone (Delta Link Expiry)

Centralized in `_delta_sync.py`:

```python
# Detection during pagination (first page only):
if exc.response.status_code == 410 or _is_sync_state_error(error_code):
    raise DeltaExpiredError(...)

# Recovery in fetcher:
try:
    result = await call_with_token_retry(token_info, _do_fetch)
except DeltaExpiredError:
    return await fetch_messages(..., delta_link=None)  # Fresh sync
```

Microsoft error codes that trigger delta expiry: `syncstatenotfound`, `resyncresquired`, `invalidrequest`.

Gmail equivalent: 400/404 on `history.list` — falls back to full message fetch.

## Circuit Breaker

Prevents repeatedly syncing integrations that are failing due to transient provider issues.

**Threshold**: 5 consecutive failures (`CIRCUIT_BREAKER_THRESHOLD` in `sync_health.py`)

**Behavior**:
- Tier 2 org tasks filter out integrations where `consecutive_sync_failures >= 5`
- Counter resets to 0 on next successful sync via `record_sync_success()`
- Does NOT set `is_active=False` — the integration remains "active" but skipped
- Different from auth failures which immediately deactivate

**Applied to**: Email org task, Calendar org task, Messaging channels org task.

## Timeout Tracking

When `SoftTimeLimitExceeded` fires, all Tier 2 and Tier 3 tasks record a sync failure:

```python
except SoftTimeLimitExceeded:
    run_async(record_sync_failure(
        organisation_id=org_id,
        integration_id=int_id,
        error=TimeoutError(f"Soft time limit exceeded ({limit}s)"),
    ))
```

Tier 2 org tasks track the currently-syncing integration via `current_integration_id` so the timeout handler knows which integration to record the failure against.

`TimeoutError` is classified as `PROVIDER_ERROR` (transient) — it increments `consecutive_sync_failures` but does not trigger `requires_reauth`.

## Sync Health Recording

After each integration sync in a Tier 2 org task:

```python
# Success path:
await record_sync_success(organisation_id=org_id, integration_id=int_id)
# Resets consecutive_sync_failures to 0

# Failure path:
await record_sync_failure(
    organisation_id=org_id,
    integration_id=int_id,
    error=original_exception,  # NOT a string — the real exception for classification
)
# Increments consecutive_sync_failures
# If AUTH_FAILED: sets requires_reauth=True, is_active=False
```

**Important**: `record_sync_failure` receives the original exception object, not an error string. This is required for `classify_sync_error()` to properly detect `AUTH_FAILED` vs `PROVIDER_ERROR` by inspecting `HTTPStatusError.response`.

## Failure Summary

| Scenario | Detection | Recovery |
|----------|-----------|----------|
| Access token expired | Proactive 5-min buffer | Auto-refresh before API call |
| Access token corrupted | 401 from API | `call_with_token_retry` refreshes and retries |
| Refresh token expired | 400 from token endpoint | `AuthTokenRevokedError` → `requires_reauth=True` |
| Refresh token revoked | `invalid_grant` / AADSTS codes | Same as above |
| 429 rate limit | HTTP 429 | 3 retries with exponential backoff + jitter |
| Delta link expired | 410 Gone / `syncstatenotfound` | Automatic fallback to full sync |
| Gmail history expired | 400/404 on history.list | Automatic fallback to full fetch |
| Network timeout | httpx timeout | Classified as `PROVIDER_ERROR`, circuit breaker |
| Provider 5xx | HTTP 500+ | Classified as `PROVIDER_ERROR`, circuit breaker |
| Task timeout | `SoftTimeLimitExceeded` | `record_sync_failure` with `TimeoutError` |
