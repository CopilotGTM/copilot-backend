"""Contracts for the channel-independent agent attachment grounding pipeline."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_grounder_isolates_one_attachment_failure() -> None:
    """A corrupt first file must not prevent a later valid file from grounding."""
    reader_module = importlib.import_module("src.service.chat.chat_attachment_reader")
    grounding_module = importlib.import_module("src.agents.attachment_grounding")
    reads: list[str] = []

    async def fake_reader(attachment_id, *, context, max_chars):
        reads.append(attachment_id)
        if attachment_id == "broken":
            raise RuntimeError("corrupt PDF")
        return reader_module.AttachmentReadResult(
            attachment_id=attachment_id,
            filename="valid.txt",
            mimetype="text/plain",
            kind="text",
            text="grounded content",
        )

    messages = [
        {
            "role": "user",
            "content": "compare",
            "attachment_manifest": (
                "[Attached files]\n"
                "- id: broken\n  name: broken.pdf\n  type: application/pdf\n"
                "- id: valid\n  name: valid.txt\n  type: text/plain"
            ),
        }
    ]

    result = await grounding_module.ground_chat_attachments(
        message_list=messages,
        context=SimpleNamespace(),
        images=None,
        attachment_text=None,
        max_items=3,
        max_text_tokens=3000,
        max_chars=12000,
        model_id="gpt-5.4",
        reader=fake_reader,
    )

    assert reads == ["broken", "valid"]
    assert result.images is None
    assert "grounded content" in result.text
    assert result.failed_ids == ["broken"]
