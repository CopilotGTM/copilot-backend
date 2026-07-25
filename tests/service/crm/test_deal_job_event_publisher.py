"""Contract tests for deal-job Redis event publication."""

from __future__ import annotations

import logging
from uuid import uuid4

import pytest
from src.service.crm.jobs import deal_job_event_publisher as publisher_module
from src.service.crm.jobs.deal_job_event_publisher import DealJobRedisPublisher


def test_publish_adds_stream_metadata_and_increments_sequence(monkeypatch) -> None:
    """Each publisher instance should emit ordered frames to its deal-job stream."""
    organisation_id = uuid4()
    job_id = uuid4()
    writes: list[tuple[str, dict, int]] = []

    def capture_write(stream_key: str, frame: dict, *, maxlen: int) -> str:
        writes.append((stream_key, dict(frame), maxlen))
        return "1-0"

    monkeypatch.setattr(publisher_module, "xadd_json_frame", capture_write)
    monkeypatch.setattr(publisher_module.time, "time", lambda: 1234.5)

    publisher = DealJobRedisPublisher(organisation_id, job_id, "deal_summary", maxlen=17)
    publisher.publish({"type": "first"})
    publisher.publish({"type": "second"})

    assert writes == [
        (
            f"deal:{organisation_id}:job:{job_id}",
            {
                "type": "first",
                "sequence": 0,
                "timestamp": 1234.5,
                "job_type": "deal_summary",
                "job_id": str(job_id),
            },
            17,
        ),
        (
            f"deal:{organisation_id}:job:{job_id}",
            {
                "type": "second",
                "sequence": 1,
                "timestamp": 1234.5,
                "job_type": "deal_summary",
                "job_id": str(job_id),
            },
            17,
        ),
    ]


@pytest.mark.parametrize(
    ("method_name", "args", "kwargs", "expected"),
    [
        (
            "publish_progress",
            ("Loading deal", "loading", 0.25),
            {},
            {
                "type": "progress.update",
                "message": "Loading deal",
                "stage": "loading",
                "progress": 0.25,
            },
        ),
        (
            "publish_result",
            ({"summary": "Done"},),
            {},
            {"type": "answer.replace", "content": {"summary": "Done"}},
        ),
        (
            "publish_error",
            ("Could not load", "load_failed"),
            {},
            {
                "type": "error",
                "message": "Could not load",
                "error_code": "load_failed",
            },
        ),
        ("publish_finalize", (), {}, {"type": "finalize", "reason": "done"}),
        (
            "publish_deal_started",
            (2, 8, "Acme"),
            {},
            {
                "type": "import.deal.started",
                "deal_index": 2,
                "row_number": 8,
                "deal_name": "Acme",
            },
        ),
        (
            "publish_deal_progress",
            (2, 8, "contacts", "Adding contacts"),
            {"deal_name": "Acme"},
            {
                "type": "import.deal.progress",
                "deal_index": 2,
                "row_number": 8,
                "deal_name": "Acme",
                "step": "contacts",
                "message": "Adding contacts",
            },
        ),
        (
            "publish_deal_completed",
            (2, 8, "created", "deal-id", "Acme"),
            {},
            {
                "type": "import.deal.completed",
                "deal_index": 2,
                "row_number": 8,
                "outcome": "created",
                "deal_id": "deal-id",
                "deal_name": "Acme",
                "warnings": [],
            },
        ),
        (
            "publish_deal_failed",
            (2, 8, "Acme", "invalid_row", "Invalid deal row"),
            {},
            {
                "type": "import.deal.failed",
                "deal_index": 2,
                "row_number": 8,
                "deal_name": "Acme",
                "error_code": "invalid_row",
                "message": "Invalid deal row",
            },
        ),
        (
            "publish_summary_tick",
            (),
            {
                "processed": 8,
                "total": 10,
                "created": 4,
                "updated": 2,
                "skipped": 1,
                "failed": 1,
                "warnings": 3,
            },
            {
                "type": "import.summary.tick",
                "processed": 8,
                "total": 10,
                "created": 4,
                "updated": 2,
                "skipped": 1,
                "failed": 1,
                "warnings": 3,
            },
        ),
    ],
)
def test_convenience_methods_preserve_frame_shapes(
    monkeypatch,
    method_name: str,
    args: tuple,
    kwargs: dict,
    expected: dict,
) -> None:
    """Convenience methods should keep their existing frontend frame contracts."""
    frames: list[dict] = []

    def capture_write(stream_key: str, frame: dict, *, maxlen: int) -> str:
        frames.append(dict(frame))
        return "1-0"

    monkeypatch.setattr(publisher_module, "xadd_json_frame", capture_write)
    monkeypatch.setattr(publisher_module.time, "time", lambda: 1234.5)
    job_id = uuid4()
    publisher = DealJobRedisPublisher(uuid4(), job_id, "test_job")

    getattr(publisher, method_name)(*args, **kwargs)

    assert frames == [
        {
            **expected,
            "sequence": 0,
            "timestamp": 1234.5,
            "job_type": "test_job",
            "job_id": str(job_id),
        }
    ]


def test_publish_warns_without_raising_when_redis_write_fails(monkeypatch, caplog) -> None:
    """Redis delivery remains best-effort while exposing a structured warning."""
    monkeypatch.setattr(
        publisher_module,
        "xadd_json_frame",
        lambda stream_key, frame, *, maxlen: None,
    )
    organisation_id = uuid4()
    job_id = uuid4()
    publisher = DealJobRedisPublisher(organisation_id, job_id, "deal_summary")

    with caplog.at_level(logging.WARNING, logger=publisher_module.__name__):
        publisher.publish({"type": "progress.update"})

    assert "Failed to publish frame to Redis stream" in caplog.messages
    record = caplog.records[-1]
    assert record.event == "deal_job_publish_failed"
    assert record.organisation_id == str(organisation_id)
    assert record.job_id == str(job_id)
    assert record.job_type == "deal_summary"
