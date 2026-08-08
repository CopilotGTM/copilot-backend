"""Runtime metric contracts for bounded labels, counts, durations, and memory."""

from src.infra.observability import metrics as metrics_module
from src.infra.observability.metrics import (
    record_celery_task_memory,
    record_celery_task_run,
    record_http_server_request,
    record_whatsapp_durable_job,
    record_whatsapp_enqueue,
    record_whatsapp_inbound,
    record_whatsapp_job_age,
    record_whatsapp_processing,
    record_whatsapp_recovery_sweep,
    record_whatsapp_send,
)


class _FakeMetric:
    def __init__(self) -> None:
        self.calls: list[tuple[object, dict[str, str]]] = []

    def add(self, value: object, attributes: dict[str, str]) -> None:
        self.calls.append((value, attributes))

    def record(self, value: object, attributes: dict[str, str]) -> None:
        self.calls.append((value, attributes))


def test_record_http_server_request_tracks_count_and_duration(monkeypatch) -> None:
    request_counter = _FakeMetric()
    duration_histogram = _FakeMetric()

    monkeypatch.setattr(
        "src.infra.observability.metrics._HTTP_SERVER_REQUEST_COUNT",
        request_counter,
    )
    monkeypatch.setattr(
        "src.infra.observability.metrics._HTTP_SERVER_REQUEST_DURATION",
        duration_histogram,
    )

    record_http_server_request(
        method="GET",
        route="/users/crm/deals/{deal_id}",
        status_code=503,
        duration_ms=187,
    )

    # Route templates and status-family labels are what downstream SLO queries depend on.
    assert request_counter.calls == [
        (
            1,
            {
                "http.method": "GET",
                "http.route": "/users/crm/deals/{deal_id}",
                "http.response.status_code": "503",
                "http.status_family": "5xx",
            },
        )
    ]
    assert duration_histogram.calls == [
        (
            187,
            {
                "http.method": "GET",
                "http.route": "/users/crm/deals/{deal_id}",
                "http.response.status_code": "503",
                "http.status_family": "5xx",
            },
        )
    ]


def test_record_celery_task_run_tracks_status_and_optional_duration(monkeypatch) -> None:
    task_counter = _FakeMetric()
    task_duration = _FakeMetric()

    monkeypatch.setattr(
        "src.infra.observability.metrics._CELERY_TASK_EXECUTION_COUNT",
        task_counter,
    )
    monkeypatch.setattr(
        "src.infra.observability.metrics._CELERY_TASK_DURATION",
        task_duration,
    )

    record_celery_task_run(
        task_name="deal.match_calendar_to_deals_org",
        queue="deal",
        status="SUCCESS",
        duration_ms=912,
    )
    record_celery_task_run(
        task_name="deal.match_calendar_to_deals_org",
        queue="deal",
        status="FAILURE",
        duration_ms=None,
    )

    # Failed tasks should still count even when no duration could be captured.
    assert task_counter.calls == [
        (
            1,
            {
                "celery.task_name": "deal.match_calendar_to_deals_org",
                "celery.queue": "deal",
                "celery.status": "SUCCESS",
            },
        ),
        (
            1,
            {
                "celery.task_name": "deal.match_calendar_to_deals_org",
                "celery.queue": "deal",
                "celery.status": "FAILURE",
            },
        ),
    ]
    assert task_duration.calls == [
        (
            912,
            {
                "celery.task_name": "deal.match_calendar_to_deals_org",
                "celery.queue": "deal",
                "celery.status": "SUCCESS",
            },
        )
    ]


def test_record_celery_task_memory_tracks_rss_and_delta_with_stable_labels(monkeypatch) -> None:
    rss_histogram = _FakeMetric()
    delta_histogram = _FakeMetric()

    monkeypatch.setattr(
        "src.infra.observability.metrics._CELERY_TASK_RSS_MB",
        rss_histogram,
    )
    monkeypatch.setattr(
        "src.infra.observability.metrics._CELERY_TASK_RSS_DELTA_MB",
        delta_histogram,
    )

    record_celery_task_memory(
        task_name="integrations.sync_hubspot_deals",
        queue="integrations",
        status="SUCCESS",
        rss_mb=512.25,
        rss_delta_mb=64.5,
    )

    expected_attributes = {
        "celery.task_name": "integrations.sync_hubspot_deals",
        "celery.queue": "integrations",
        "celery.status": "SUCCESS",
    }
    assert rss_histogram.calls == [(512.25, expected_attributes)]
    assert delta_histogram.calls == [(64.5, expected_attributes)]


def test_whatsapp_delivery_metrics_use_only_bounded_attributes(monkeypatch) -> None:
    metric_names = [
        "_WHATSAPP_INBOUND_COUNT",
        "_WHATSAPP_DURABLE_JOB_COUNT",
        "_WHATSAPP_ENQUEUE_COUNT",
        "_WHATSAPP_RECOVERY_SWEEP_COUNT",
        "_WHATSAPP_JOB_AGE",
        "_WHATSAPP_PROCESSING_COUNT",
        "_WHATSAPP_SEND_COUNT",
        "_WHATSAPP_SEND_ATTEMPTS",
    ]
    metrics = {name: _FakeMetric() for name in metric_names}
    for name, metric in metrics.items():
        monkeypatch.setattr(f"src.infra.observability.metrics.{name}", metric, raising=False)

    record_whatsapp_inbound("new")
    record_whatsapp_durable_job("reused")
    record_whatsapp_enqueue("requeued")
    record_whatsapp_recovery_sweep("completed")
    record_whatsapp_job_age("pending", 125.5)
    record_whatsapp_processing("agent_failed")
    record_whatsapp_send("retryable", attempts=2)

    assert metrics["_WHATSAPP_INBOUND_COUNT"].calls == [(1, {"outcome": "new"})]
    assert metrics["_WHATSAPP_DURABLE_JOB_COUNT"].calls == [(1, {"outcome": "reused"})]
    assert metrics["_WHATSAPP_ENQUEUE_COUNT"].calls == [(1, {"outcome": "requeued"})]
    assert metrics["_WHATSAPP_RECOVERY_SWEEP_COUNT"].calls == [(1, {"outcome": "completed"})]
    assert metrics["_WHATSAPP_JOB_AGE"].calls == [(125.5, {"status": "pending"})]
    assert metrics["_WHATSAPP_PROCESSING_COUNT"].calls == [(1, {"outcome": "agent_failed"})]
    assert metrics["_WHATSAPP_SEND_COUNT"].calls == [(1, {"outcome": "retryable"})]
    assert metrics["_WHATSAPP_SEND_ATTEMPTS"].calls == [(2, {"outcome": "failed"})]


def test_whatsapp_metric_rejects_identifier_as_bounded_unknown(monkeypatch) -> None:
    inbound = _FakeMetric()
    monkeypatch.setattr(
        "src.infra.observability.metrics._WHATSAPP_INBOUND_COUNT",
        inbound,
        raising=False,
    )

    record_whatsapp_inbound("wamid.15551234567.job-id")

    assert inbound.calls == [(1, {"outcome": "unknown"})]


def test_agent_delivery_metrics_are_bounded_and_contain_no_business_ids(monkeypatch) -> None:
    assert hasattr(metrics_module, "record_agent_run_delivery")
    count = _FakeMetric()
    attempts = _FakeMetric()
    latency = _FakeMetric()
    monkeypatch.setattr(metrics_module, "_AGENT_RUN_DELIVERY_COUNT", count, raising=False)
    monkeypatch.setattr(metrics_module, "_AGENT_RUN_DELIVERY_ATTEMPTS", attempts, raising=False)
    monkeypatch.setattr(metrics_module, "_AGENT_RUN_DELIVERY_LATENCY", latency, raising=False)

    metrics_module.record_agent_run_delivery(
        "needs_attention",
        provider="gmail-user-id-123",
        reason="email_send_status_unknown",
        attempts=2,
        latency_ms=850,
    )

    attributes = {
        "channel": "email",
        "outcome": "needs_attention",
        "provider": "unknown",
        "reason": "unknown",
    }
    assert count.calls == [(1, attributes)]
    assert attempts.calls == [(2, attributes)]
    assert latency.calls == [(850, attributes)]


def test_agent_delivery_recovery_metrics_are_bounded_and_id_free(monkeypatch) -> None:
    recovery = _FakeMetric()
    age = _FakeMetric()
    monkeypatch.setattr(
        metrics_module,
        "_AGENT_RUN_DELIVERY_RECOVERY_COUNT",
        recovery,
        raising=False,
    )
    monkeypatch.setattr(
        metrics_module,
        "_AGENT_RUN_DELIVERY_RECOVERY_AGE",
        age,
        raising=False,
    )

    metrics_module.record_agent_run_delivery_recovery("tenant-run-id", rows=3)
    metrics_module.record_agent_run_delivery_recovery_age("mailbox-id", 42.5)

    assert recovery.calls == [(3, {"outcome": "unknown"})]
    assert age.calls == [(42.5, {"status": "unknown"})]
