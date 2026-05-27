from src.infra.observability.metrics import (
    record_celery_task_memory,
    record_celery_task_run,
    record_http_server_request,
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
