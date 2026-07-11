"""Shared environment gates for deployed E2E suites."""

from __future__ import annotations

import os

import pytest


def required_env(name: str) -> str:
    """Return a required E2E setting or skip when live credentials are absent."""
    value = os.getenv(name)
    if value:
        return value
    pytest.skip(f"{name} is required for deployed E2E tests")
