"""Sentry runtime hook shared by FastAPI and all Celery workers."""

from __future__ import annotations

import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration
from src.infra.config.configs import sentry_config

_sentry_initialized = False


def init_sentry() -> None:
    """Initialize Sentry once when a DSN is configured for the process."""
    global _sentry_initialized
    if _sentry_initialized:
        return

    if not sentry_config.SENTRY_DSN:
        return

    sentry_sdk.init(
        dsn=sentry_config.SENTRY_DSN,
        environment=sentry_config.SENTRY_ENVIRONMENT,
        send_default_pii=sentry_config.SENTRY_SEND_DEFAULT_PII,
        traces_sample_rate=sentry_config.SENTRY_TRACES_SAMPLE_RATE,
        integrations=[FastApiIntegration(), CeleryIntegration()],
    )
    _sentry_initialized = True
