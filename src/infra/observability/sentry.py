"""Sentry runtime hook shared by FastAPI and all Celery workers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.infra.config.configs import sentry_config

_sentry_initialized = False
_REDACTED = "[Filtered]"
# Key matching normalizes punctuation/case so variants like ``client_secret``,
# ``x-api-key``, and Supabase's ``apikey`` are treated consistently.
_SENSITIVE_KEY_FRAGMENTS = frozenset(
    ("apikey", "authorization", "cookie", "key", "password", "secret", "session", "token")
)


def _is_sensitive_key(key: object) -> bool:
    normalized_key = "".join(ch for ch in str(key).lower() if ch.isalnum())
    return any(fragment in normalized_key for fragment in _SENSITIVE_KEY_FRAGMENTS)


def _scrub_mapping(value: object) -> object:
    if isinstance(value, Mapping):
        scrubbed: dict[object, object] = {}
        for key, item in value.items():
            scrubbed[key] = _REDACTED if _is_sensitive_key(key) else _scrub_mapping(item)
        return scrubbed

    if isinstance(value, list):
        return [_scrub_mapping(item) for item in value]

    if isinstance(value, tuple):
        return tuple(_scrub_mapping(item) for item in value)

    return value


def _scrub_request(request: dict[str, Any]) -> None:
    headers = request.get("headers")
    if isinstance(headers, dict):
        for key in list(headers):
            if _is_sensitive_key(key):
                headers[key] = _REDACTED

    for field in ("data", "cookies"):
        if field in request:
            request[field] = _scrub_mapping(request[field])

    # Query strings occasionally carry API keys from webhook/dev traffic.
    if "query_string" in request:
        request["query_string"] = _REDACTED


def _scrub_breadcrumbs(event: dict[str, Any]) -> None:
    breadcrumbs = event.get("breadcrumbs")
    values = breadcrumbs.get("values") if isinstance(breadcrumbs, dict) else breadcrumbs

    if not isinstance(values, list):
        return

    for crumb in values:
        if isinstance(crumb, dict) and "data" in crumb:
            crumb["data"] = _scrub_mapping(crumb["data"])


def _scrub_event(event: Any, hint: Mapping[str, object]) -> Any:
    del hint

    # Scrub in place — Sentry treats the returned event as the final payload.
    request = event.get("request")
    if isinstance(request, dict):
        _scrub_request(request)

    user = event.get("user")
    if isinstance(user, dict) and "ip_address" in user:
        user["ip_address"] = _REDACTED

    if "extra" in event:
        event["extra"] = _scrub_mapping(event["extra"])

    if "contexts" in event:
        event["contexts"] = _scrub_mapping(event["contexts"])

    _scrub_breadcrumbs(event)

    return event


def init_sentry() -> None:
    """Initialize Sentry once when a DSN is configured for the process."""
    global _sentry_initialized
    if _sentry_initialized:
        return

    if not sentry_config.SENTRY_DSN:
        return

    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.fastapi import FastApiIntegration

    sentry_sdk.init(
        dsn=sentry_config.SENTRY_DSN,
        environment=sentry_config.SENTRY_ENVIRONMENT,
        send_default_pii=sentry_config.SENTRY_SEND_DEFAULT_PII,
        traces_sample_rate=sentry_config.SENTRY_TRACES_SAMPLE_RATE,
        # Final redaction step before events leave the process.
        before_send=_scrub_event,
        integrations=[FastApiIntegration(), CeleryIntegration()],
    )
    _sentry_initialized = True
