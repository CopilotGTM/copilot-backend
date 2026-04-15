from pathlib import Path


def test_chat_worker_subscribes_to_voice_realtime_queue() -> None:
    compose_path = Path(__file__).resolve().parents[3] / "docker-compose.yml"
    compose_text = compose_path.read_text(encoding="utf-8")

    expected = "--queues=attachments,chat_processing,chat_messages,voice_realtime"
    assert expected in compose_text
