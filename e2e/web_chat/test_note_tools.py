"""Deployed WebChat checks for GeneralAgent deal-note write tools."""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest

from e2e.crm_notes.support import (
    cleanup_notes,
    deal_name,
    require_note_target,
    wait_for_note,
)
from e2e.web_chat.support import (
    auth_headers,
    base_url,
    require_live_delivery_target,
    session_id,
    wait_for_job,
)

pytestmark = pytest.mark.e2e


def _send_and_wait(client: httpx.Client, prompt: str) -> dict:
    """Send one real WebChat prompt and require successful agent completion."""
    response = client.post(
        f"{base_url()}/users/chats/send_message",
        headers=auth_headers(),
        json={"session_id": str(session_id()), "content": prompt},
    )
    assert response.status_code == 200, response.text

    job_id = UUID(response.json()["data"]["job_id"])
    job = wait_for_job(
        job_id,
        lambda current: current["status"] in {"completed", "failed"},
        expected_state="a terminal note-tool state",
    )
    assert job["status"] == "completed", job
    return job


def test_web_chat_creates_then_updates_one_deal_note() -> None:
    """Prove WebChat invokes create and update without duplicating the note row."""
    require_live_delivery_target()
    require_note_target("web_chat")
    marker = f"e2e-web-note-{uuid4()}"
    original_body = f"Original WebChat note body {uuid4()}"
    updated_body = f"Updated WebChat note body {uuid4()}"
    created_note: dict | None = None

    try:
        with httpx.Client(timeout=30) as client:
            create_job = _send_and_wait(
                client,
                f"Add a note titled '{marker}' to deal '{deal_name()}'. "
                f"The note body must be exactly: {original_body}",
            )
            created_note = wait_for_note(
                "web_chat",
                marker,
                lambda notes: len(notes) == 1 and notes[0]["body"] == original_body,
                expected_state="one newly created note",
            )[0]
            create_text = create_job["assistant_content"]["text"]
            assert str(created_note["note_id"]) in create_text
            assert str(created_note["deal_id"]) in create_text

            update_job = _send_and_wait(
                client,
                f"On deal '{deal_name()}', update the note titled '{marker}'. "
                f"Change only its body to exactly: {updated_body}",
            )
            updated_notes = wait_for_note(
                "web_chat",
                marker,
                lambda notes: len(notes) == 1 and notes[0]["body"] == updated_body,
                expected_state="the same note with its updated body",
            )
            update_text = update_job["assistant_content"]["text"]
            assert str(updated_notes[0]["note_id"]) in update_text
            assert str(updated_notes[0]["deal_id"]) in update_text

        assert updated_notes[0]["note_id"] == created_note["note_id"]
        assert updated_notes[0]["title"] == marker
    finally:
        cleanup_notes("web_chat", marker)
