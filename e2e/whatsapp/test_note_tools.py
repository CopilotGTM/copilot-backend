"""Deployed WhatsApp checks for GeneralAgent deal-note write tools."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from e2e.crm_notes.support import (
    cleanup_notes,
    deal_name,
    require_note_target,
    wait_for_note,
)
from e2e.whatsapp.support import (
    encoded_payload,
    meta_text_payload,
    require_live_delivery_target,
    signed_headers,
    wait_for_job,
    webhook_url,
)

pytestmark = pytest.mark.e2e


def _send_and_wait(prompt: str) -> dict:
    """Send one signed Meta callback and require successful agent delivery."""
    payload = meta_text_payload(text=prompt)
    message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    body = encoded_payload(payload)

    with httpx.Client(timeout=30) as client:
        response = client.post(webhook_url(), content=body, headers=signed_headers(body))
    assert response.status_code == 200, response.text

    job = wait_for_job(
        str(message["id"]),
        lambda current: current["status"] in {"completed", "failed"},
        expected_state="a terminal WhatsApp note-tool state",
    )
    assert job["status"] == "completed", job
    assert job["job_metadata"]["agent_success"] is True
    assert job["job_metadata"]["send_status"] == "sent"
    return job


def test_whatsapp_creates_then_updates_one_deal_note() -> None:
    """Prove WhatsApp invokes create and update without duplicating the note row."""
    require_live_delivery_target()
    require_note_target("whatsapp")
    marker = f"e2e-whatsapp-note-{uuid4()}"
    original_body = f"Original WhatsApp note body {uuid4()}"
    updated_body = f"Updated WhatsApp note body {uuid4()}"
    created_note: dict | None = None

    try:
        create_job = _send_and_wait(
            f"Add a note titled '{marker}' to deal '{deal_name()}'. "
            f"The note body must be exactly: {original_body}"
        )
        created_note = wait_for_note(
            "whatsapp",
            marker,
            lambda notes: len(notes) == 1 and notes[0]["body"] == original_body,
            expected_state="one newly created note",
        )[0]
        create_text = "\n".join(
            chunk["text"] for chunk in create_job["job_metadata"]["outbound_chunks"]
        )
        assert str(created_note["note_id"]) in create_text
        assert str(created_note["deal_id"]) in create_text

        update_job = _send_and_wait(
            f"On deal '{deal_name()}', update the note titled '{marker}'. "
            f"Change only its body to exactly: {updated_body}"
        )
        updated_notes = wait_for_note(
            "whatsapp",
            marker,
            lambda notes: len(notes) == 1 and notes[0]["body"] == updated_body,
            expected_state="the same note with its updated body",
        )
        update_text = "\n".join(
            chunk["text"] for chunk in update_job["job_metadata"]["outbound_chunks"]
        )
        assert str(updated_notes[0]["note_id"]) in update_text
        assert str(updated_notes[0]["deal_id"]) in update_text

        assert updated_notes[0]["note_id"] == created_note["note_id"]
        assert updated_notes[0]["title"] == marker
    finally:
        cleanup_notes("whatsapp", marker)
