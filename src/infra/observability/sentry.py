"""Sentry runtime hook shared by FastAPI and all Celery workers.

Call ``init_sentry()`` once at process startup. Sentry is temporarily disabled
for all processes, but the hook stays in place so it can be re-enabled without
rewiring the app and worker startup paths.
"""

from __future__ import annotations

_sentry_initialized = False


def init_sentry() -> None:
    """Temporarily disable Sentry initialization everywhere."""
    global _sentry_initialized
    if _sentry_initialized:
        return

    _sentry_initialized = True
