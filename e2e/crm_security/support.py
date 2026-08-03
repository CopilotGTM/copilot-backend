"""Shared actors, safety gates, HTTP clients, SSE reads, and durable CRM probes."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from e2e.conftest import e2e_access_token_for, required_env

_TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}


@dataclass(frozen=True)
class CRMActor:
    """One named deployed user participating in an authorization scenario."""

    name: str
    access_token: str

    @classmethod
    def from_environment(cls, name: str, prefix: str) -> CRMActor:
        return cls(
            name=name,
            access_token=e2e_access_token_for(
                f"{prefix}_EMAIL",
                f"{prefix}_PASSWORD",
            ),
        )

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }


@dataclass(frozen=True)
class SSEEvent:
    """One parsed server-sent event from the deployed job stream."""

    event: str
    data: dict[str, Any] | str | None
    event_id: str | None = None


def base_url() -> str:
    return required_env("E2E_API_BASE_URL").rstrip("/")


def require_crm_live_target() -> None:
    """Allow CRM mutations only on the explicitly named and allowlisted dev host."""
    if os.getenv("E2E_CRM_ENABLE_LIVE_MUTATION", "").lower() != "true":
        pytest.skip("E2E_CRM_ENABLE_LIVE_MUTATION=true is required for CRM live scenarios")

    target = required_env("E2E_TARGET_ENVIRONMENT").strip().lower()
    if target != "dev":
        pytest.fail(f"CRM live E2E requires target environment 'dev', got {target!r}")

    hostname = (urlparse(base_url()).hostname or "").lower()
    allowed_hosts = {
        item.strip().lower()
        for item in required_env("E2E_ALLOWED_MUTATION_HOSTS").split(",")
        if item.strip()
    }
    if hostname not in allowed_hosts:
        pytest.fail(f"CRM live E2E host {hostname!r} is not explicitly allowlisted")


def configured_uuid(name: str) -> UUID:
    try:
        return UUID(required_env(name))
    except ValueError:
        pytest.fail(f"{name} must contain a UUID")


def post(actor: CRMActor, path: str) -> httpx.Response:
    with httpx.Client(timeout=30) as client:
        return client.post(f"{base_url()}{path}", headers=actor.headers)


def get(actor: CRMActor, path: str) -> httpx.Response:
    with httpx.Client(timeout=30) as client:
        return client.get(f"{base_url()}{path}", headers=actor.headers)


def read_job_stream(
    actor: CRMActor,
    path: str,
    *,
    last_event_id: str | None = None,
) -> list[SSEEvent]:
    """Read one bounded deployed SSE stream through its terminal event."""
    headers = dict(actor.headers)
    if last_event_id:
        headers["Last-Event-ID"] = last_event_id

    read_timeout = float(os.getenv("E2E_CRM_STREAM_TIMEOUT_SECONDS", "240"))
    events: list[SSEEvent] = []
    with (
        httpx.Client(timeout=httpx.Timeout(15, read=read_timeout)) as client,
        client.stream("GET", f"{base_url()}{path}", headers=headers) as response,
    ):
        if response.status_code != 200:
            body = response.read().decode(errors="replace")
            pytest.fail(f"SSE request failed with HTTP {response.status_code}: {body}")
        fields: dict[str, str] = {}
        for line in response.iter_lines():
            if line:
                key, separator, value = line.partition(":")
                if separator:
                    fields[key] = value.lstrip()
                continue
            if not fields:
                continue
            raw_data = fields.get("data")
            try:
                data = json.loads(raw_data) if raw_data else None
            except json.JSONDecodeError:
                data = raw_data
            event = SSEEvent(
                event=fields.get("event", "message"),
                data=data,
                event_id=fields.get("id"),
            )
            events.append(event)
            fields = {}
            if event.event in {"complete", "error", "timeout"}:
                break
    return events


def _database_url() -> str:
    return required_env("E2E_CRM_DATABASE_URL").replace("postgresql+asyncpg://", "postgresql://", 1)


def _tenant_schema() -> sql.Identifier:
    return sql.Identifier(os.getenv("E2E_TENANT_SCHEMA", "tenant_data"))


def fetch_job(organisation_id: UUID, job_id: UUID) -> dict[str, Any] | None:
    """Read one exact tenant/job identity through a read-only database connection."""
    statement = sql.SQL(
        """
        SELECT job_id, organisation_id, user_id, deal_id, event_id, job_type,
               status, result_data, error_message, created_at, updated_at
        FROM {}.deal_jobs
        WHERE organisation_id = %s AND job_id = %s
        """
    ).format(_tenant_schema())
    with psycopg2.connect(_database_url(), connect_timeout=10) as connection:
        connection.set_session(readonly=True, autocommit=True)
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(statement, (str(organisation_id), str(job_id)))
            row = cursor.fetchone()
            return dict(row) if row else None


def count_jobs_since(
    organisation_id: UUID,
    deal_id: UUID,
    *,
    since: datetime,
) -> int:
    statement = sql.SQL(
        """
        SELECT count(*)
        FROM {}.deal_jobs
        WHERE organisation_id = %s AND deal_id = %s AND created_at >= %s
        """
    ).format(_tenant_schema())
    with psycopg2.connect(_database_url(), connect_timeout=10) as connection:
        connection.set_session(readonly=True, autocommit=True)
        with connection.cursor() as cursor:
            cursor.execute(statement, (str(organisation_id), str(deal_id), since))
            return int(cursor.fetchone()[0])


def wait_for_terminal_job(organisation_id: UUID, job_id: UUID) -> dict[str, Any]:
    timeout_seconds = float(os.getenv("E2E_CRM_JOB_TIMEOUT_SECONDS", "240"))
    poll_seconds = float(os.getenv("E2E_CRM_POLL_SECONDS", "2"))
    deadline = time.monotonic() + timeout_seconds
    last_job: dict[str, Any] | None = None

    while time.monotonic() < deadline:
        last_job = fetch_job(organisation_id, job_id)
        if last_job and last_job["status"] in _TERMINAL_JOB_STATUSES:
            return last_job
        time.sleep(poll_seconds)

    summary = (
        {"status": last_job["status"], "error_message": last_job["error_message"]}
        if last_job
        else "job not found"
    )
    pytest.fail(f"CRM job {job_id} did not reach a terminal state: {summary}")


def run_started_at() -> datetime:
    """Return a database-comparable UTC boundary for denied-mutation assertions."""
    return datetime.now(UTC)
