"""Safety gates, provider fixtures, queue dispatch, and durable Gmail probes."""

from __future__ import annotations

import base64
import os
import ssl
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from functools import cache
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
import psycopg2
import pytest
from celery import Celery
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from e2e.conftest import required_env


def require_gmail_live_target() -> None:
    """Allow provider and queue mutations only for the allowlisted dev fixture."""
    if os.getenv("E2E_GMAIL_ENABLE_LIVE_MUTATION", "").lower() != "true":
        pytest.skip("E2E_GMAIL_ENABLE_LIVE_MUTATION=true is required")
    if required_env("E2E_TARGET_ENVIRONMENT").strip().lower() != "dev":
        pytest.fail("Gmail live E2E may run only against dev")

    hostname = (urlparse(required_env("E2E_API_BASE_URL")).hostname or "").lower()
    allowed_hosts = {
        item.strip().lower()
        for item in required_env("E2E_ALLOWED_MUTATION_HOSTS").split(",")
        if item.strip()
    }
    if hostname not in allowed_hosts:
        pytest.fail(f"Gmail live E2E host {hostname!r} is not explicitly allowlisted")

    broker_hostname = (urlparse(required_env("E2E_INTEGRATIONS_BROKER_URL")).hostname or "").lower()
    allowed_brokers = {
        item.strip().lower()
        for item in required_env("E2E_ALLOWED_BROKER_HOSTS").split(",")
        if item.strip()
    }
    if broker_hostname not in allowed_brokers:
        pytest.fail(f"Gmail live E2E broker {broker_hostname!r} is not explicitly allowlisted")


def configured_uuid(name: str) -> UUID:
    """Load an exact fixture identifier without inventing a fallback record."""
    try:
        return UUID(required_env(name))
    except ValueError:
        pytest.fail(f"{name} must contain a UUID")


def _database_url() -> str:
    return required_env("E2E_GMAIL_DATABASE_URL").replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )


def _tenant_schema() -> sql.Identifier:
    return sql.Identifier(os.getenv("E2E_TENANT_SCHEMA", "tenant_data"))


def _query_one(statement: sql.Composed, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
    with psycopg2.connect(_database_url(), connect_timeout=10) as connection:
        connection.set_session(readonly=True, autocommit=True)
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(statement, parameters)
            row = cursor.fetchone()
            return dict(row) if row else None


def fetch_sync_state(organisation_id: UUID, integration_id: UUID) -> dict[str, Any] | None:
    """Read the effective Gmail capability cursor and completion time."""
    statement = sql.SQL(
        """
        SELECT i.integration_id, i.organisation_id, i.integration_type,
               COALESCE(s.cursor_metadata, i.connection_metadata) AS cursor_metadata,
               COALESCE(s.last_synced_at, i.last_synced_at) AS last_synced_at,
               COALESCE(s.last_sync_status, i.last_sync_status) AS last_sync_status,
               COALESCE(s.last_sync_error, i.last_sync_error) AS last_sync_error
        FROM {}.knowledge_integrations i
        LEFT JOIN {}.integration_sync_states s
          ON s.organisation_id = i.organisation_id
         AND s.integration_id = i.integration_id
         AND s.capability = 'gmail'
         AND s.enabled = true
        WHERE i.organisation_id = %s AND i.integration_id = %s
        """
    ).format(_tenant_schema(), _tenant_schema())
    return _query_one(statement, (str(organisation_id), str(integration_id)))


def fetch_email(
    organisation_id: UUID,
    integration_id: UUID,
    external_message_id: str,
) -> dict[str, Any] | None:
    """Read one email through its full tenant, integration, and provider identity."""
    statement = sql.SQL(
        """
        SELECT e.email_id, e.external_message_id, e.subject, e.is_deleted,
               e.labels, e.has_attachments, f.external_folder_id
        FROM {}.emails e
        JOIN {}.email_folders f
          ON f.organisation_id = e.organisation_id
         AND f.folder_id = e.folder_id
        WHERE e.organisation_id = %s
          AND e.integration_id = %s
          AND e.external_message_id = %s
        """
    ).format(_tenant_schema(), _tenant_schema())
    return _query_one(
        statement,
        (str(organisation_id), str(integration_id), external_message_id),
    )


def count_email_identity(
    organisation_id: UUID,
    integration_id: UUID,
    external_message_id: str,
) -> int:
    """Count the exact tenant-scoped identity used by the email upsert constraint."""
    statement = sql.SQL(
        """
        SELECT count(*) AS count
        FROM {}.emails
        WHERE organisation_id = %s
          AND integration_id = %s
          AND external_message_id = %s
        """
    ).format(_tenant_schema())
    row = _query_one(statement, (str(organisation_id), str(integration_id), external_message_id))
    return int(row["count"]) if row else 0


@cache
def _celery_client() -> Celery:
    broker_url = required_env("E2E_INTEGRATIONS_BROKER_URL")
    client = Celery("gmail_e2e", broker=broker_url, backend=broker_url)
    if urlparse(broker_url).scheme == "rediss":
        ssl_options = {"ssl_cert_reqs": ssl.CERT_REQUIRED}
        client.conf.broker_use_ssl = ssl_options
        client.conf.redis_backend_use_ssl = ssl_options
    return client


def dispatch_email_sync(organisation_id: UUID, integration_id: UUID) -> str:
    """Enqueue the same deployed single-integration task used by webhook recovery."""
    client = _celery_client()
    result = client.send_task(
        "integrations.sync_email_integration",
        args=[str(organisation_id), str(integration_id)],
        kwargs={"trigger_source": "e2e_gmail"},
        queue="integrations",
        routing_key="integrations",
    )
    return str(result.id)


def wait_for_task(task_id: str) -> dict[str, Any]:
    """Require the exact enqueued worker task to return a successful result."""
    timeout = float(os.getenv("E2E_GMAIL_TASK_TIMEOUT_SECONDS", "1080"))
    result = _celery_client().AsyncResult(task_id).get(timeout=timeout)
    assert isinstance(result, dict), result
    assert result.get("status") == "success", result
    return result


def wait_for_sync_after(
    organisation_id: UUID,
    integration_id: UUID,
    *,
    started_at: datetime,
) -> dict[str, Any]:
    """Wait for durable cursor metadata written after the message transaction commits."""
    deadline = time.monotonic() + float(os.getenv("E2E_GMAIL_SYNC_TIMEOUT_SECONDS", "360"))
    poll_seconds = float(os.getenv("E2E_GMAIL_POLL_SECONDS", "2"))
    last_state: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last_state = fetch_sync_state(organisation_id, integration_id)
        completed_at = last_state.get("last_synced_at") if last_state else None
        if completed_at and completed_at.replace(tzinfo=UTC) > started_at:
            return last_state
        time.sleep(poll_seconds)
    pytest.fail(f"Gmail sync did not complete after {started_at.isoformat()}: {last_state}")


@dataclass(frozen=True)
class SeededMessage:
    """Track the exact provider IDs required for bounded fixture cleanup."""

    message_id: str
    draft_id: str | None = None


class GmailSeeder:
    """Mutate only a dedicated provider mailbox using separately consented test scopes."""

    def __init__(self) -> None:
        token_response = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": required_env("E2E_GMAIL_SEEDER_CLIENT_ID"),
                "client_secret": required_env("E2E_GMAIL_SEEDER_CLIENT_SECRET"),
                "refresh_token": required_env("E2E_GMAIL_SEEDER_REFRESH_TOKEN"),
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        token_response.raise_for_status()
        self._client = httpx.Client(
            base_url="https://gmail.googleapis.com/gmail/v1/users/me",
            headers={"Authorization": f"Bearer {token_response.json()['access_token']}"},
            timeout=30,
        )
        self._mailbox = required_env("E2E_GMAIL_SEEDER_EMAIL")

    def close(self) -> None:
        """Release the provider connection after fixture cleanup finishes."""
        self._client.close()

    def _raw_message(self, subject: str, *, attachment: bool = False) -> str:
        message = EmailMessage()
        message["From"] = self._mailbox
        message["To"] = self._mailbox
        message["Subject"] = subject
        message["Date"] = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S %z")
        message.set_content(f"Dedicated CopilotGTM E2E fixture: {subject}")
        if attachment:
            message.add_attachment(b"e2e", maintype="text", subtype="plain", filename="e2e.txt")
        return base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")

    def insert(self, subject: str, labels: list[str], *, attachment: bool = False) -> SeededMessage:
        """Insert a synthetic message with explicit starting Gmail labels."""
        response = self._client.post(
            "/messages",
            params={"internalDateSource": "dateHeader"},
            json={"raw": self._raw_message(subject, attachment=attachment), "labelIds": labels},
        )
        response.raise_for_status()
        return SeededMessage(message_id=response.json()["id"])

    def send(self, subject: str) -> SeededMessage:
        """Send a synthetic message so Gmail itself assigns the SENT system label."""
        response = self._client.post(
            "/messages/send",
            json={"raw": self._raw_message(subject)},
        )
        response.raise_for_status()
        return SeededMessage(message_id=response.json()["id"])

    def create_draft(self, subject: str) -> SeededMessage:
        """Create a real draft instead of fabricating the provider-owned DRAFT label."""
        response = self._client.post(
            "/drafts",
            json={"message": {"raw": self._raw_message(subject)}},
        )
        response.raise_for_status()
        payload = response.json()
        return SeededMessage(message_id=payload["message"]["id"], draft_id=payload["id"])

    def modify_labels(
        self,
        message_id: str,
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> None:
        """Create a Gmail history event by changing labels on one exact message."""
        response = self._client.post(
            f"/messages/{message_id}/modify",
            json={"addLabelIds": add or [], "removeLabelIds": remove or []},
        )
        response.raise_for_status()

    def trash(self, message_id: str) -> None:
        """Move one fixture outside the ingestion policy without permanent deletion."""
        response = self._client.post(f"/messages/{message_id}/trash")
        response.raise_for_status()

    def cleanup(self, seeded: SeededMessage) -> None:
        """Remove only the exact fixture while tolerating already-removed provider state."""
        if seeded.draft_id:
            response = self._client.delete(f"/drafts/{seeded.draft_id}")
            if response.status_code not in {204, 404}:
                response.raise_for_status()
            return
        response = self._client.post(f"/messages/{seeded.message_id}/trash")
        if response.status_code != 404:
            response.raise_for_status()
