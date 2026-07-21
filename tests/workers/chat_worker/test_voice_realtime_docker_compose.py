from pathlib import Path

import yaml


def test_chat_worker_subscribes_to_voice_realtime_queue() -> None:
    compose_path = Path(__file__).resolve().parents[3] / "docker-compose.yml"
    compose_text = compose_path.read_text(encoding="utf-8")

    expected = "--queues=attachments,chat_processing,chat_messages,voice_realtime"
    assert expected in compose_text


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
