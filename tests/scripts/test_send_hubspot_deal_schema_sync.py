from __future__ import annotations

from uuid import uuid4

import send_hubspot_deal_schema_sync as script


class _FakeResult:
    id = "task-123"


class _FakeApp:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def send_task(self, task_name: str, **kwargs):
        self.calls.append({"task_name": task_name, **kwargs})
        return _FakeResult()


def test_main_dispatches_hubspot_schema_sync(monkeypatch, capsys) -> None:
    fake_app = _FakeApp()
    monkeypatch.setattr(script, "_get_integrations_app", lambda: fake_app)

    organisation_id = str(uuid4())
    integration_id = str(uuid4())

    exit_code = script.main([organisation_id, integration_id])

    assert exit_code == 0
    assert fake_app.calls == [
        {
            "task_name": script.TASK_NAME,
            "kwargs": {
                "organisation_id": organisation_id,
                "integration_id": integration_id,
            },
            "queue": script.QUEUE_NAME,
            "routing_key": script.ROUTING_KEY,
        }
    ]
    assert capsys.readouterr().out.strip() == "task-123"
