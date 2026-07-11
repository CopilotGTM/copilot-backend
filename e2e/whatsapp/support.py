"""Shared request and read-only delivery probes for deployed WhatsApp E2E tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from e2e.conftest import required_env

_PRODUCTION_ENVIRONMENTS = {"prod", "production"}


def base_url() -> str:
    """Return the deployed API root without a trailing slash."""
    return required_env("E2E_API_BASE_URL").rstrip("/")


def webhook_url() -> str:
    """Return the public WhatsApp webhook endpoint for the deployed target."""
    return f"{base_url()}/users/bots/whatsapp/webhook"


def signed_headers(body: bytes) -> dict[str, str]:
    """Sign one webhook body exactly as Meta signs production callbacks."""
    app_secret = required_env("E2E_WHATSAPP_APP_SECRET")
    digest = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={digest}",
    }


def meta_text_payload(
    *,
    message_id: str | None = None,
    text: str = "E2E hello",
    phone_number_id: str | None = None,
) -> dict[str, Any]:
    """Build the Meta text-message shape consumed by the deployed webhook."""
    configured_phone_number_id = required_env("E2E_WHATSAPP_PHONE_NUMBER_ID")
    recipient_wa_id = required_env("E2E_WHATSAPP_RECIPIENT_WA_ID")
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "e2e-business-account",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550000000",
                                "phone_number_id": phone_number_id or configured_phone_number_id,
                            },
                            "contacts": [
                                {
                                    "profile": {"name": "E2E WhatsApp User"},
                                    "wa_id": recipient_wa_id,
                                }
                            ],
                            "messages": [
                                {
                                    "from": recipient_wa_id,
                                    "id": message_id or f"wamid.e2e.{uuid4()}",
                                    "timestamp": str(int(time.time())),
                                    "text": {"body": text},
                                    "type": "text",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def encoded_payload(payload: dict[str, Any]) -> bytes:
    """Serialize a webhook deterministically so its signature matches its body."""
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def require_live_delivery_target() -> None:
    """Require explicit non-production opt-in before creating real jobs or sends."""
    if os.getenv("E2E_WHATSAPP_ENABLE_LIVE_DELIVERY", "").lower() != "true":
        pytest.skip("E2E_WHATSAPP_ENABLE_LIVE_DELIVERY=true is required for live delivery")

    for name in (
        "E2E_API_BASE_URL",
        "E2E_WHATSAPP_APP_SECRET",
        "E2E_WHATSAPP_PHONE_NUMBER_ID",
        "E2E_WHATSAPP_RECIPIENT_WA_ID",
        "E2E_WHATSAPP_DATABASE_URL",
        "E2E_WHATSAPP_ORG_SCHEMA",
    ):
        required_env(name)

    target = required_env("E2E_TARGET_ENVIRONMENT").lower()
    if target in _PRODUCTION_ENVIRONMENTS or target.startswith("prod-"):
        pytest.fail("WhatsApp E2E delivery tests must use a dedicated non-production target")


def fetch_jobs(message_id: str) -> list[dict[str, Any]]:
    """Read jobs for one provider message from the dedicated org schema."""
    database_url = required_env("E2E_WHATSAPP_DATABASE_URL").replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )
    org_schema = required_env("E2E_WHATSAPP_ORG_SCHEMA")
    statement = sql.SQL(
        """
        SELECT job_id, status, error_message, idempotency_key, job_metadata, created_at, updated_at
        FROM {}.chat_message_jobs
        WHERE chat_type = 'WHATSAPP_CHAT'
          AND job_metadata->>'whatsapp_message_id' = %s
        ORDER BY created_at
        """
    ).format(sql.Identifier(org_schema))

    # The E2E credential is read-only because assertions must never mutate a
    # deployed tenant while inspecting delivery evidence.
    with psycopg2.connect(database_url, connect_timeout=10) as connection:
        connection.set_session(readonly=True, autocommit=True)
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(statement, (message_id,))
            return [dict(row) for row in cursor.fetchall()]


def wait_for_job(
    message_id: str,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    expected_state: str,
) -> dict[str, Any]:
    """Poll the durable ledger until one job reaches the expected observable state."""
    timeout_seconds = float(os.getenv("E2E_WHATSAPP_DELIVERY_TIMEOUT_SECONDS", "180"))
    poll_seconds = float(os.getenv("E2E_WHATSAPP_POLL_SECONDS", "2"))
    deadline = time.monotonic() + timeout_seconds
    last_job: dict[str, Any] | None = None

    while time.monotonic() < deadline:
        jobs = fetch_jobs(message_id)
        if jobs:
            last_job = jobs[-1]
            if predicate(last_job):
                return last_job
        time.sleep(poll_seconds)

    summary = _job_summary(last_job) if last_job else "no durable job found"
    pytest.fail(f"WhatsApp job did not reach {expected_state}: {summary}")


def _job_summary(job: dict[str, Any]) -> str:
    """Keep timeout diagnostics useful without printing the full stored envelope."""
    metadata = job.get("job_metadata") or {}
    return str(
        {
            "status": job.get("status"),
            "error_message": job.get("error_message"),
            "enqueue_status": metadata.get("enqueue_status"),
            "processing_stage": metadata.get("processing_stage"),
            "send_status": metadata.get("send_status"),
            "send_attempts": metadata.get("send_attempts"),
            "last_send_status_code": metadata.get("last_send_status_code"),
        }
    )
