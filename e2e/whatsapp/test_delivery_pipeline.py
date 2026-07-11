"""Deployed WhatsApp delivery checks from signed webhook to durable Meta outcome."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest

from e2e.whatsapp.support import (
    encoded_payload,
    fetch_jobs,
    meta_text_payload,
    require_live_delivery_target,
    signed_headers,
    wait_for_job,
    webhook_url,
)

pytestmark = pytest.mark.e2e

_WHATSAPP_TEXT_CHUNK_LIMIT = 3900


def _message_id(payload: dict[str, Any]) -> str:
    return str(payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"])


def _post(payload: dict[str, Any]) -> httpx.Response:
    body = encoded_payload(payload)
    with httpx.Client(timeout=30) as client:
        return client.post(webhook_url(), content=body, headers=signed_headers(body))


def _assert_meta_accepted_output(job: dict[str, Any]) -> None:
    metadata = job["job_metadata"]
    chunks = metadata["outbound_chunks"]

    assert job["status"] == "completed"
    assert job["error_message"] is None
    assert metadata["agent_success"] is True
    assert metadata["processing_stage"] == "completed"
    assert metadata["send_status"] == "sent"
    assert metadata["send_attempts"] >= 1
    assert chunks
    assert [chunk["index"] for chunk in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert isinstance(chunk["text"], str) and chunk["text"].strip()
        assert len(chunk["text"]) <= _WHATSAPP_TEXT_CHUNK_LIMIT
        assert isinstance(chunk["provider_message_id"], str)
        assert chunk["provider_message_id"].strip()
        assert chunk["accepted_at"]
        assert 200 <= chunk["status_code"] < 300


def test_text_message_reaches_meta_with_complete_input_and_output_evidence() -> None:
    """Prove the deployed path preserves input and records Meta-accepted output."""
    require_live_delivery_target()
    prompt = f"Reply briefly to this WhatsApp E2E probe: {uuid4()}"
    payload = meta_text_payload(text=prompt)
    message_id = _message_id(payload)

    response = _post(payload)

    assert response.status_code == 200
    assert response.content == b""
    job = wait_for_job(
        message_id,
        lambda current: current["status"] in {"completed", "failed"},
        expected_state="a terminal delivery state",
    )
    metadata = job["job_metadata"]
    envelope = metadata["dispatch_envelope"]

    # Exact input and routing data must survive intake, persistence, and worker recovery.
    assert metadata["whatsapp_message_id"] == message_id
    assert metadata["idempotency_key"].endswith(f":{message_id}")
    assert job["idempotency_key"] == metadata["idempotency_key"]
    assert metadata["enqueue_status"] in {"enqueued", "requeued"}
    assert envelope["provider"] == "whatsapp"
    assert envelope["message_text"] == prompt
    assert envelope["reply_context"]["incoming_message_id"] == message_id
    assert (
        envelope["reply_context"]["phone_number_id"]
        == payload["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"]
    )
    _assert_meta_accepted_output(job)


def test_duplicate_webhook_creates_one_job_and_one_durable_output() -> None:
    """Prove a provider retry converges to one job and one accepted answer."""
    require_live_delivery_target()
    payload = meta_text_payload(
        message_id=f"wamid.e2e.duplicate.{uuid4()}",
        text="Reply briefly to this duplicate-delivery E2E probe.",
    )
    message_id = _message_id(payload)

    first = _post(payload)
    retry = _post(payload)

    assert first.status_code == 200
    assert retry.status_code == 200
    job = wait_for_job(
        message_id,
        lambda current: current["status"] in {"completed", "failed"},
        expected_state="a terminal duplicate-delivery state",
    )
    jobs = fetch_jobs(message_id)

    assert len(jobs) == 1
    _assert_meta_accepted_output(job)


def test_meta_rejection_is_recorded_as_a_terminal_delivery_failure() -> None:
    """Prove a real Meta rejection is terminal, visible, and not reported as sent."""
    require_live_delivery_target()
    invalid_phone_number_id = f"e2e-invalid-{uuid4().hex}"
    payload = meta_text_payload(
        text="Reply briefly to this outbound-failure E2E probe.",
        phone_number_id=invalid_phone_number_id,
    )
    message_id = _message_id(payload)

    response = _post(payload)

    assert response.status_code == 200
    job = wait_for_job(
        message_id,
        lambda current: (
            current["status"] == "failed"
            and (current["job_metadata"] or {}).get("failure_stage") == "sending"
        ),
        expected_state="a terminal Meta send failure",
    )
    metadata = job["job_metadata"]

    # The real Meta rejection must stay visible instead of becoming a completed job.
    assert job["status"] == "failed"
    assert job["error_message"]
    assert metadata["agent_success"] is True
    assert metadata["processing_stage"] == "failed"
    assert metadata["failure_stage"] == "sending"
    assert metadata["send_status"] == "terminal"
    assert metadata["send_attempts"] == 1
    assert 400 <= metadata["last_send_status_code"] < 500
    assert metadata["last_send_error"]
    assert metadata["dispatch_envelope"]["reply_context"]["phone_number_id"] == (
        invalid_phone_number_id
    )
    assert all(chunk["provider_message_id"] is None for chunk in metadata["outbound_chunks"])
