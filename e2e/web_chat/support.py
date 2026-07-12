"""Shared request and read-only database probes for deployed web-chat E2E tests."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from e2e.conftest import e2e_access_token, required_env

_PRODUCTION_ENVIRONMENTS = {"prod", "production"}


def base_url() -> str:
    """Return the deployed API root without a trailing slash."""
    return required_env("E2E_API_BASE_URL").rstrip("/")


def auth_headers() -> dict[str, str]:
    """Build the authenticated headers for the dedicated E2E user."""
    return {
        "Authorization": f"Bearer {e2e_access_token()}",
        "Content-Type": "application/json",
    }


def require_live_delivery_target() -> None:
    """Require explicit non-production opt-in before creating chat messages."""
    if os.getenv("E2E_WEB_CHAT_ENABLE_LIVE_DELIVERY", "").lower() != "true":
        pytest.skip("E2E_WEB_CHAT_ENABLE_LIVE_DELIVERY=true is required for live delivery")

    for name in (
        "E2E_API_BASE_URL",
        "E2E_USER_EMAIL",
        "E2E_USER_PASSWORD",
        "E2E_WEB_CHAT_SESSION_ID",
        "E2E_WEB_CHAT_DATABASE_URL",
        "E2E_WEB_CHAT_ORG_SCHEMA",
    ):
        required_env(name)

    target = required_env("E2E_TARGET_ENVIRONMENT").lower()
    if target in _PRODUCTION_ENVIRONMENTS or target.startswith("prod-"):
        pytest.fail("Web-chat E2E delivery tests require a dedicated non-production target")


def session_id() -> UUID:
    """Return the existing chat session owned by the dedicated E2E user."""
    return UUID(required_env("E2E_WEB_CHAT_SESSION_ID"))


def fetch_job(job_id: UUID) -> dict[str, Any] | None:
    """Read one job with its persisted input and output messages."""
    database_url = required_env("E2E_WEB_CHAT_DATABASE_URL").replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )
    org_schema = required_env("E2E_WEB_CHAT_ORG_SCHEMA")
    statement = sql.SQL(
        """
        SELECT
            job.job_id,
            job.status,
            job.error_message,
            job.job_metadata,
            job.result_data,
            user_message.content AS user_content,
            assistant_message.content AS assistant_content,
            job.created_at,
            job.updated_at
        FROM {}.chat_message_jobs AS job
        JOIN {}.chat_messages AS user_message
          ON user_message.message_id = job.message_id
        LEFT JOIN {}.chat_messages AS assistant_message
          ON assistant_message.message_id =
             (job.job_metadata->>'assistant_message_id')::uuid
        WHERE job.job_id = %s
        """
    ).format(
        sql.Identifier(org_schema),
        sql.Identifier(org_schema),
        sql.Identifier(org_schema),
    )

    # E2E assertions inspect deployed state but never mutate a tenant.
    with psycopg2.connect(database_url, connect_timeout=10) as connection:
        connection.set_session(readonly=True, autocommit=True)
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(statement, (str(job_id),))
            row = cursor.fetchone()
            return dict(row) if row else None


def wait_for_job(
    job_id: UUID,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    expected_state: str,
) -> dict[str, Any]:
    """Poll until the deployed job reaches the requested observable state."""
    timeout_seconds = float(os.getenv("E2E_WEB_CHAT_DELIVERY_TIMEOUT_SECONDS", "180"))
    poll_seconds = float(os.getenv("E2E_WEB_CHAT_POLL_SECONDS", "2"))
    deadline = time.monotonic() + timeout_seconds
    last_job: dict[str, Any] | None = None

    while time.monotonic() < deadline:
        last_job = fetch_job(job_id)
        if last_job and predicate(last_job):
            return last_job
        time.sleep(poll_seconds)

    summary = (
        {
            "status": last_job.get("status"),
            "error_message": last_job.get("error_message"),
            "enqueue_status": (last_job.get("job_metadata") or {}).get("enqueue_status"),
        }
        if last_job
        else "no durable job found"
    )
    pytest.fail(f"Web-chat job did not reach {expected_state}: {summary}")
