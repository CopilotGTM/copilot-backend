from src.adapters import AgentResponse
from src.infra.messaging.chat_stream_models import AnswerDelta, AnswerReplace


def test_answer_replace_overwrites_prior_deltas() -> None:
    response = AgentResponse.from_frames(
        [
            AnswerDelta(content="How can I help you?"),
            AnswerReplace(content="How can I help you?"),
        ]
    )

    assert response.text == "How can I help you?"


def test_answer_deltas_still_concatenate_without_replace() -> None:
    response = AgentResponse.from_frames(
        [
            AnswerDelta(content="How can "),
            AnswerDelta(content="I help you?"),
        ]
    )

    assert response.text == "How can I help you?"


def test_answer_delta_after_replace_appends_to_replacement_only() -> None:
    response = AgentResponse.from_frames(
        [
            AnswerDelta(content="old partial"),
            AnswerReplace(content="final"),
            AnswerDelta(content=" text"),
        ]
    )

    assert response.text == "final text"
