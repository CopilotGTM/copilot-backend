from __future__ import annotations

from types import SimpleNamespace

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
