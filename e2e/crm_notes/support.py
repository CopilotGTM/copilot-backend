"""Read-only CRM-note probes and authenticated cleanup for deployed E2E tests.

Channel tests own message delivery. This module only locates notes created in a
dedicated non-production tenant and removes them through the public API, so E2E
runs do not retain synthetic CRM data.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

import httpx
import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from e2e.conftest import e2e_access_token, required_env

Channel = Literal["web_chat", "whatsapp"]

_CHANNEL_DATABASE_ENV: dict[Channel, tuple[str, str]] = {
    "web_chat": ("E2E_WEB_CHAT_DATABASE_URL", "E2E_WEB_CHAT_ORG_SCHEMA"),
    "whatsapp": ("E2E_WHATSAPP_DATABASE_URL", "E2E_WHATSAPP_ORG_SCHEMA"),
}


def require_note_target(channel: Channel) -> None:
    """Require the disposable deal, login, and channel database configuration."""
    database_env, schema_env = _CHANNEL_DATABASE_ENV[channel]
    for name in (
        "E2E_API_BASE_URL",
        "E2E_USER_EMAIL",
        "E2E_USER_PASSWORD",
        "E2E_CRM_DEAL_ID",
        "E2E_CRM_DEAL_NAME",
        database_env,
        schema_env,
    ):
        required_env(name)


def deal_id() -> UUID:
    """Return the dedicated internal CRM deal used for note mutations."""
    return UUID(required_env("E2E_CRM_DEAL_ID"))


def deal_name() -> str:
    """Return the exact deal name the GeneralAgent must resolve."""
    return required_env("E2E_CRM_DEAL_NAME")


def fetch_notes(channel: Channel, marker: str) -> list[dict[str, Any]]:
    """Read active notes matching one unique test title from the target deal."""
    database_env, schema_env = _CHANNEL_DATABASE_ENV[channel]
    database_url = required_env(database_env).replace("postgresql+asyncpg://", "postgresql://", 1)
    statement = sql.SQL(
        """
        SELECT note_id, deal_id, title, body, source_modified_at, note_metadata
        FROM {}.crm_notes
        WHERE deal_id = %s
          AND title = %s
          AND (is_deleted = FALSE OR is_deleted IS NULL)
        ORDER BY source_created_at, note_id
        """
    ).format(sql.Identifier(required_env(schema_env)))

    # The database credential remains read-only; cleanup goes through the same
    # authenticated public contract a product client uses.
    with psycopg2.connect(database_url, connect_timeout=10) as connection:
        connection.set_session(readonly=True, autocommit=True)
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(statement, (str(deal_id()), marker))
            return [dict(row) for row in cursor.fetchall()]


def wait_for_note(
    channel: Channel,
    marker: str,
    predicate: Callable[[list[dict[str, Any]]], bool],
    *,
    expected_state: str,
) -> list[dict[str, Any]]:
    """Poll deployed CRM state until the note mutation becomes observable."""
    timeout_seconds = float(os.getenv("E2E_CRM_NOTE_TIMEOUT_SECONDS", "180"))
    poll_seconds = float(os.getenv("E2E_CRM_NOTE_POLL_SECONDS", "2"))
    deadline = time.monotonic() + timeout_seconds
    last_notes: list[dict[str, Any]] = []

    while time.monotonic() < deadline:
        last_notes = fetch_notes(channel, marker)
        if predicate(last_notes):
            return last_notes
        time.sleep(poll_seconds)

    summary = [
        {"note_id": str(note["note_id"]), "title": note["title"], "body": note["body"]}
        for note in last_notes
    ]
    pytest.fail(f"CRM note did not reach {expected_state}: {summary}")


def cleanup_note(target_deal_id: UUID, note_id: UUID) -> None:
    """Soft-delete one synthetic note through the deployed authenticated API."""
    response = httpx.delete(
        (
            f"{required_env('E2E_API_BASE_URL').rstrip('/')}"
            f"/users/crm/deals/{target_deal_id}/notes/{note_id}"
        ),
        headers={"Authorization": f"Bearer {e2e_access_token()}"},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "SUCCESS", payload


def cleanup_notes(channel: Channel, marker: str) -> None:
    """Remove every active note for a unique marker, including partial-test output."""
    for note in fetch_notes(channel, marker):
        cleanup_note(UUID(str(note["deal_id"])), UUID(str(note["note_id"])))


__all__ = [
    "cleanup_note",
    "cleanup_notes",
    "deal_id",
    "deal_name",
    "fetch_notes",
    "require_note_target",
    "wait_for_note",
]
