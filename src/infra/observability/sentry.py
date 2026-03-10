"""Sentry SDK initialization shared by FastAPI and all Celery workers.

Call ``init_sentry()`` once at process startup — before the FastAPI app or
Celery app is created. Duplicate calls are safe (Sentry SDK ignores re-init).

Disabled automatically when SENTRY_DSN is empty (local dev).
"""

from __future__ import annotations

import os

_sentry_initialized = False


def init_sentry() -> None:
    """Initialize Sentry SDK if SENTRY_DSN is configured."""
    global _sentry_initialized
    if _sentry_initialized:
        return

    from src.infra.config.configs import sentry_config

    dsn = sentry_config.SENTRY_DSN
    if not dsn:
        return

    import sentry_sdk
    from sentry_sdk.integrations.httpx import HttpxIntegration

    sentry_sdk.init(
        dsn=dsn,
        environment=sentry_config.SENTRY_ENVIRONMENT,
        traces_sample_rate=sentry_config.SENTRY_TRACES_SAMPLE_RATE,
        send_default_pii=sentry_config.SENTRY_SEND_DEFAULT_PII,
        # Sentry's httpx patching breaks supabase-py auth calls in our API runtime.
        # Keep Sentry enabled, but leave outbound httpx transport behavior untouched.
        disabled_integrations=[HttpxIntegration()],
        # Attach server name so Sentry distinguishes API vs worker instances
        server_name=os.getenv("SENTRY_SERVER_NAME"),
    )

    _sentry_initialized = True
