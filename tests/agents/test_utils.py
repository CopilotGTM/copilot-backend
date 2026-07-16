from __future__ import annotations

from types import SimpleNamespace

from src.agents.attachment_grounding import (
    automatic_attachment_items,
    truncate_to_token_budget,
)
from src.agents.utils import _MAX_ATTACHMENT_TEXT_CHARS, prepare_messages

# ---------------------------------------------------------------------------
# prepare_messages — basic message extraction
# ---------------------------------------------------------------------------


def test_prepare_messages_from_dict_messages() -> None:
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]
    result = prepare_messages(messages)

    assert len(result) == 2
    assert result[0].role == "user"
    assert result[0].content == "Hello"
    assert result[1].role == "assistant"
    assert result[1].content == "Hi there"


def test_prepare_messages_from_chat_message_objects() -> None:
    msg = SimpleNamespace(role="user", content="What's up?")
    result = prepare_messages([msg])

    assert len(result) == 1
    assert result[0].content == "What's up?"


def test_prepare_messages_handles_dict_content() -> None:
    messages = [{"role": "user", "content": {"text": "nested content"}}]
    result = prepare_messages(messages)

    assert result[0].content == "nested content"


def test_prepare_messages_defaults_role_to_user() -> None:
    messages = [{"content": "no role specified"}]
    result = prepare_messages(messages)

    assert result[0].role == "user"


# ---------------------------------------------------------------------------
# prepare_messages — attachment manifest per message
# ---------------------------------------------------------------------------


def test_prepare_messages_appends_manifest_to_message() -> None:
    messages = [
        {
            "role": "user",
            "content": "Check this file",
            "attachment_manifest": "[Attached files]\n- id: abc\n  name: doc.pdf",
        }
    ]
    result = prepare_messages(messages)

    assert "[Attached files]" in result[0].content
    assert "Check this file" in result[0].content


def test_prepare_messages_manifest_replaces_empty_content() -> None:
    messages = [
        {
            "role": "user",
            "content": "",
            "attachment_manifest": "[Attached files]\n- id: abc",
        }
    ]
    result = prepare_messages(messages)

    assert result[0].content == "[Attached files]\n- id: abc"


def test_prepare_messages_no_manifest_leaves_content_alone() -> None:
    messages = [{"role": "user", "content": "Just text", "attachment_manifest": None}]
    result = prepare_messages(messages)

    assert result[0].content == "Just text"


# ---------------------------------------------------------------------------
# prepare_messages — legacy attachment_text fallback
# ---------------------------------------------------------------------------


def test_prepare_messages_appends_attachment_text_to_last_user_message() -> None:
    messages = [
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "Answer"},
        {"role": "user", "content": "Follow up"},
    ]
    result = prepare_messages(messages, attachment_text="[file content here]")

    assert "[file content here]" in result[2].content
    assert "[file content here]" not in result[0].content


def test_prepare_messages_attachment_text_skips_assistant_messages() -> None:
    messages = [
        {"role": "user", "content": "Question"},
        {"role": "assistant", "content": "Response"},
    ]
    result = prepare_messages(messages, attachment_text="attached")

    assert "attached" in result[0].content
    assert "attached" not in result[1].content


def test_prepare_messages_attachment_text_on_empty_user_message() -> None:
    messages = [{"role": "user", "content": ""}]
    result = prepare_messages(messages, attachment_text="file content")

    assert result[0].content == "file content"


def test_prepare_messages_no_attachment_text_when_none() -> None:
    messages = [{"role": "user", "content": "Hello"}]
    result = prepare_messages(messages, attachment_text=None)

    assert result[0].content == "Hello"


def test_prepare_messages_empty_attachment_text_ignored() -> None:
    messages = [{"role": "user", "content": "Hello"}]
    result = prepare_messages(messages, attachment_text="")

    assert result[0].content == "Hello"


# ---------------------------------------------------------------------------
# prepare_messages — truncation
# ---------------------------------------------------------------------------


def test_prepare_messages_truncates_long_attachment_text() -> None:
    long_text = "x" * (_MAX_ATTACHMENT_TEXT_CHARS + 1000)
    messages = [{"role": "user", "content": "Q"}]
    result = prepare_messages(messages, attachment_text=long_text)

    content = result[0].content
    assert "[... content truncated due to length ...]" in content
    assert len(content) < len(long_text) + 100


def test_prepare_messages_does_not_truncate_within_limit() -> None:
    text = "x" * (_MAX_ATTACHMENT_TEXT_CHARS - 10)
    messages = [{"role": "user", "content": "Q"}]
    result = prepare_messages(messages, attachment_text=text)

    assert "[... content truncated" not in result[0].content


# ---------------------------------------------------------------------------
# prepare_messages — empty list
# ---------------------------------------------------------------------------


def test_prepare_messages_empty_list() -> None:
    assert prepare_messages([]) == []


def test_prepare_messages_empty_list_with_attachment_text() -> None:
    result = prepare_messages([], attachment_text="some text")
    assert result == []


def test_automatic_attachment_items_prefers_current_user_attachments() -> None:
    messages = [
        {
            "role": "user",
            "content": "first",
            "attachment_manifest": (
                "[Attached files]\n- id: old-id\n  name: old.pdf\n  type: application/pdf"
            ),
        },
        {"role": "assistant", "content": "first response"},
        {
            "role": "user",
            "content": "second",
            "attachment_manifest": (
                "[Attached files]\n- id: current-id\n  name: current.png\n  type: image/png"
            ),
        },
    ]

    assert [item.attachment_id for item in automatic_attachment_items(messages, max_items=3)] == [
        "current-id"
    ]


def test_automatic_attachment_items_carries_previous_user_attachments_once() -> None:
    messages = [
        {
            "role": "user",
            "content": "first",
            "attachment_manifest": (
                "[Attached files]\n- id: prior-id\n  name: prior.pdf\n  type: application/pdf"
            ),
        },
        {"role": "assistant", "content": "first response"},
        {"role": "user", "content": "follow up", "attachment_manifest": None},
    ]

    assert [item.attachment_id for item in automatic_attachment_items(messages, max_items=3)] == [
        "prior-id"
    ]


def test_automatic_attachment_items_expires_after_one_empty_user_turn() -> None:
    messages = [
        {
            "role": "user",
            "content": "first",
            "attachment_manifest": (
                "[Attached files]\n- id: expired-id\n  name: old.pdf\n  type: application/pdf"
            ),
        },
        {"role": "assistant", "content": "first response"},
        {"role": "user", "content": "second", "attachment_manifest": None},
        {"role": "assistant", "content": "second response"},
        {"role": "user", "content": "third", "attachment_manifest": None},
    ]

    assert automatic_attachment_items(messages, max_items=3) == []


def test_automatic_attachment_items_caps_before_callers_filter_types() -> None:
    messages = [
        {
            "role": "user",
            "content": "compare these",
            "attachment_manifest": (
                "[Attached files]\n"
                "- id: one\n  name: one.mp3\n  type: audio/mpeg\n"
                "- id: two\n  name: two.pdf\n  type: application/pdf\n"
                "- id: three\n  name: three.png\n  type: image/png\n"
                "- id: four\n  name: four.txt\n  type: text/plain"
            ),
        },
    ]

    assert [item.attachment_id for item in automatic_attachment_items(messages, max_items=3)] == [
        "one",
        "two",
        "three",
    ]


def test_automatic_attachment_items_keeps_missing_type_for_safe_caller_skip() -> None:
    messages = [
        {
            "role": "user",
            "content": "old manifest",
            "attachment_manifest": "[Attached files]\n- id: legacy-id\n  name: file.pdf",
        }
    ]

    items = automatic_attachment_items(messages, max_items=3)
    assert len(items) == 1
    assert items[0].attachment_id == "legacy-id"
    assert items[0].mimetype == ""


def test_truncate_to_token_budget_counts_model_tokens(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.agents.attachment_grounding.count_text_tokens",
        lambda text, _model_id: len(text),
    )

    content, truncated, used = truncate_to_token_budget("abcdefgh", 5, "model")

    assert content == "abcde"
    assert truncated is True
    assert used == 5
