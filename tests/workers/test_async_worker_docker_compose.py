"""Keep local worker topology aligned with the production queue split."""

from pathlib import Path

import yaml


def _services() -> dict:
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    return yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"]


def test_integrations_worker_keeps_existing_salesforce_streaming_queue() -> None:
    services = _services()
    command = services["integrations-worker"]["command"]

    assert "--queues=integrations,integrations_priority,integrations_streaming" in command
    assert "--concurrency=4" in command
    assert "--pool=threads" in command


def test_does_not_add_a_second_salesforce_cdc_worker() -> None:
    assert "salesforce-cdc-legacy-worker" not in _services()


def test_deal_and_insights_queues_have_separate_thread_pools() -> None:
    services = _services()
    deal_command = services["deal-worker"]["command"]
    insights_command = services["deal-insights-worker"]["command"]

    assert "--queues=deal" in deal_command
    assert "deal_jobs" not in deal_command
    assert "--concurrency=4" in deal_command
    assert "--pool=threads" in deal_command

    assert "--queues=deal_jobs" in insights_command
    assert "--concurrency=2" in insights_command
    assert "--pool=threads" in insights_command
