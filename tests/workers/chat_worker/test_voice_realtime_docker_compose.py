from pathlib import Path

import yaml


def test_chat_worker_subscribes_to_all_chat_queues_with_ten_slots() -> None:
    compose_path = Path(__file__).resolve().parents[3] / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    command = compose["services"]["chat-worker"]["command"]

    expected = "--queues=attachments,chat_processing,chat_messages,voice_realtime"
    assert expected in command
    assert "--concurrency=10" in command
    assert "--pool=threads" in command
    assert "chat-message-worker" not in compose["services"]


def test_integrations_worker_subscribes_to_salesforce_streaming_queue() -> None:
    compose_path = Path(__file__).resolve().parents[3] / "docker-compose.yml"
    compose_text = compose_path.read_text(encoding="utf-8")

    expected = "--queues=integrations,integrations_priority,integrations_streaming"
    assert expected in compose_text


def test_chat_profile_does_not_start_the_beat_scheduler() -> None:
    compose_path = Path(__file__).resolve().parents[3] / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    assert compose["services"]["chat-worker"]["profiles"] == ["chat"]
    assert compose["services"]["chat-beat"]["profiles"] == ["scheduled"]
