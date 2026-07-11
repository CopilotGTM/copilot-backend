"""Deployed web-chat checks from authenticated HTTP input to persisted output."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from e2e.web_chat.support import (
    auth_headers,
    base_url,
    require_live_delivery_target,
    session_id,
    wait_for_job,
)

pytestmark = pytest.mark.e2e


def _post_message(client: httpx.Client, prompt: str) -> dict[str, str]:
    response = client.post(
        f"{base_url()}/users/chats/send_message",
        headers=auth_headers(),
        json={"session_id": str(session_id()), "content": prompt},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "SUCCESS"
    assert payload["message"] == "Message accepted"
    assert set(payload["data"]) == {"job_id", "user_message_id", "assistant_message_id"}
    return payload["data"]


def _assert_completed_output(
    job: dict[str, Any],
    *,
    prompt: str,
    identifiers: dict[str, str],
) -> None:
    metadata = job["job_metadata"]
    result = job["result_data"] or {}
    answer = result["answer"]

    assert job["status"] == "completed"
    assert job["error_message"] is None
    assert metadata["enqueue_status"] in {"enqueued", "requeued"}
    assert metadata["enqueue_attempts"] >= 1
    assert metadata["celery_task_id"]
    assert metadata["last_enqueue_error"] is None
    assert metadata["user_message_id"] == identifiers["user_message_id"]
    assert metadata["assistant_message_id"] == identifiers["assistant_message_id"]
    assert job["user_content"] == {"text": prompt}
    assert isinstance(answer, str) and answer.strip()
    assert job["assistant_content"] == {"text": answer}
    assert result["assistant_message_id"] == identifiers["assistant_message_id"]


def test_message_preserves_input_and_completes_with_durable_output() -> None:
    """Prove the deployed API, queue, worker, and persistence path end to end."""
    require_live_delivery_target()
    prompt = f"Reply briefly to this web-chat E2E probe: {uuid4()}"

    with httpx.Client(timeout=30) as client:
        identifiers = _post_message(client, prompt)
    job_id = UUID(identifiers["job_id"])

    job = wait_for_job(
        job_id,
        lambda current: current["status"] in {"completed", "failed"},
        expected_state="a terminal processing state",
    )
    _assert_completed_output(job, prompt=prompt, identifiers=identifiers)


def test_rapid_messages_each_complete_with_their_own_durable_output() -> None:
    """Prove a small burst does not lose or merge independently accepted messages."""
    require_live_delivery_target()
    prompts = [f"Reply briefly to web-chat E2E burst {index}: {uuid4()}" for index in range(3)]

    with httpx.Client(timeout=30) as client:
        accepted = [_post_message(client, prompt) for prompt in prompts]

    assert len({item["job_id"] for item in accepted}) == len(prompts)
    assert len({item["user_message_id"] for item in accepted}) == len(prompts)
    assert len({item["assistant_message_id"] for item in accepted}) == len(prompts)

    for prompt, identifiers in zip(prompts, accepted, strict=True):
        job = wait_for_job(
            UUID(identifiers["job_id"]),
            lambda current: current["status"] in {"completed", "failed"},
            expected_state="a terminal processing state for every accepted burst message",
        )
        _assert_completed_output(job, prompt=prompt, identifiers=identifiers)
