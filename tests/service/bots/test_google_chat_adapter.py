from src.adapters import AgentResponse, ResponseType, get_adapter
from src.adapters.google_chat_adapter import GoogleChatAdapter


def test_get_adapter_supports_google_chat() -> None:
    assert isinstance(get_adapter("google_chat"), GoogleChatAdapter)


def test_render_text_returns_google_chat_message_text() -> None:
    payload = GoogleChatAdapter().render_text("Hello **world**")

    assert payload.type == "message"
    assert payload.content == {"text": "Hello world"}
    assert payload.text_fallback == "Hello world"


def test_render_question_includes_fields_as_text() -> None:
    payload = GoogleChatAdapter().render(
        AgentResponse(
            text="Which deal?",
            type=ResponseType.QUESTION,
            data={"fields": [{"label": "Deal name"}]},
        )
    )

    assert "Which deal?" in payload.content["text"]
    assert "Deal name" in payload.content["text"]


def test_render_table_uses_plain_text_rows() -> None:
    payload = GoogleChatAdapter().render_table(
        "Deals",
        ["Name", "Stage"],
        [{"Name": "Acme", "Stage": "Discovery"}],
        None,
    )

    assert "Deals" in payload.content["text"]
    assert "Name: Acme" in payload.content["text"]
    assert "|" not in payload.content["text"]
