"""Lazy OpenTelemetry instruments with bounded, low-cardinality attributes."""

from __future__ import annotations

from threading import Lock
from typing import Any

from opentelemetry import metrics

# Instruments are created lazily on first use so they bind to the MeterProvider
# installed by init_otel() rather than the noop provider present at import time.
_HTTP_SERVER_REQUEST_COUNT: Any = None
_HTTP_SERVER_REQUEST_DURATION: Any = None
_CELERY_TASK_EXECUTION_COUNT: Any = None
_CELERY_TASK_DURATION: Any = None
_CELERY_TASK_RSS_MB: Any = None
_CELERY_TASK_RSS_DELTA_MB: Any = None
_WHATSAPP_INBOUND_COUNT: Any = None
_WHATSAPP_DURABLE_JOB_COUNT: Any = None
_WHATSAPP_ENQUEUE_COUNT: Any = None
_WHATSAPP_RECOVERY_SWEEP_COUNT: Any = None
_WHATSAPP_JOB_AGE: Any = None
_WHATSAPP_PROCESSING_COUNT: Any = None
_WHATSAPP_SEND_COUNT: Any = None
_WHATSAPP_SEND_ATTEMPTS: Any = None
_AGENT_RUN_DELIVERY_COUNT: Any = None
_AGENT_RUN_DELIVERY_ATTEMPTS: Any = None
_AGENT_RUN_DELIVERY_LATENCY: Any = None
_AGENT_RUN_DELIVERY_RECOVERY_COUNT: Any = None
_AGENT_RUN_DELIVERY_RECOVERY_AGE: Any = None

_init_lock = Lock()

_WHATSAPP_INBOUND_OUTCOMES = frozenset({"new", "duplicate", "invalid", "unlinked"})
_WHATSAPP_DURABLE_JOB_OUTCOMES = frozenset({"created", "reused", "failed"})
_WHATSAPP_ENQUEUE_OUTCOMES = frozenset({"enqueued", "failed", "requeued", "exhausted"})
_WHATSAPP_RECOVERY_OUTCOMES = frozenset({"completed", "failed"})
_WHATSAPP_JOB_STATUSES = frozenset({"pending", "in_progress"})
_WHATSAPP_PROCESSING_OUTCOMES = frozenset({"completed", "agent_failed", "worker_failed"})
_WHATSAPP_SEND_OUTCOMES = frozenset({"sent", "retryable", "terminal", "ambiguous"})
# Keep delivery dimensions bounded and identifier-free; run, tenant, and recipient
# IDs would make the metrics backend high-cardinality and unsuitable for SLOs.
_AGENT_RUN_DELIVERY_OUTCOMES = frozenset(
    {"created", "sent", "failed", "needs_attention", "retry", "skipped"}
)
_AGENT_RUN_DELIVERY_PROVIDERS = frozenset({"resend", "unknown"})
_AGENT_RUN_DELIVERY_RECOVERY_OUTCOMES = frozenset({"scanned", "reclaimed", "dispatched", "skipped"})
_AGENT_RUN_DELIVERY_RECOVERY_STATUSES = frozenset({"pending", "sending"})
_AGENT_RUN_DELIVERY_REASONS = frozenset(
    {
        "none",
        "delivery_outcome_unknown",
        "empty_output",
        "invalid_delivery_config",
        "invalid_recipient",
        "platform_email_not_configured",
        "recipient_unavailable",
    }
)


def _ensure_http_instruments() -> None:
    global _HTTP_SERVER_REQUEST_COUNT, _HTTP_SERVER_REQUEST_DURATION
    if _HTTP_SERVER_REQUEST_COUNT is not None:
        return
    with _init_lock:
        if _HTTP_SERVER_REQUEST_COUNT is not None:
            return
        # Single meter namespace so dashboards and SLO queries discover all
        # first-party runtime metrics without per-module registration drift.
        meter = metrics.get_meter("copilotgtm.observability")
        _HTTP_SERVER_REQUEST_COUNT = meter.create_counter(
            name="copilotgtm.http.server.request.count",
            description="Total HTTP requests handled by the API.",
            unit="{request}",
        )
        _HTTP_SERVER_REQUEST_DURATION = meter.create_histogram(
            name="copilotgtm.http.server.request.duration",
            description="HTTP request duration in milliseconds.",
            unit="ms",
        )


def _ensure_celery_instruments() -> None:
    global _CELERY_TASK_EXECUTION_COUNT, _CELERY_TASK_DURATION
    global _CELERY_TASK_RSS_MB, _CELERY_TASK_RSS_DELTA_MB
    if (
        _CELERY_TASK_EXECUTION_COUNT is not None
        and _CELERY_TASK_DURATION is not None
        and _CELERY_TASK_RSS_MB is not None
        and _CELERY_TASK_RSS_DELTA_MB is not None
    ):
        return
    with _init_lock:
        if (
            _CELERY_TASK_EXECUTION_COUNT is not None
            and _CELERY_TASK_DURATION is not None
            and _CELERY_TASK_RSS_MB is not None
            and _CELERY_TASK_RSS_DELTA_MB is not None
        ):
            return
        meter = metrics.get_meter("copilotgtm.observability")
        if _CELERY_TASK_EXECUTION_COUNT is None:
            _CELERY_TASK_EXECUTION_COUNT = meter.create_counter(
                name="copilotgtm.celery.task.execution.count",
                description="Total Celery task executions by outcome.",
                unit="{task}",
            )
        if _CELERY_TASK_DURATION is None:
            _CELERY_TASK_DURATION = meter.create_histogram(
                name="copilotgtm.celery.task.duration",
                description="Celery task execution duration in milliseconds.",
                unit="ms",
            )
        if _CELERY_TASK_RSS_MB is None:
            _CELERY_TASK_RSS_MB = meter.create_histogram(
                name="copilotgtm.celery.task.rss",
                description="Celery task process resident set size at task completion.",
                unit="MiBy",
            )
        if _CELERY_TASK_RSS_DELTA_MB is None:
            _CELERY_TASK_RSS_DELTA_MB = meter.create_histogram(
                name="copilotgtm.celery.task.rss.delta",
                description="Celery task process resident set size change during task execution.",
                unit="MiBy",
            )


def _ensure_whatsapp_instruments() -> None:
    global _WHATSAPP_INBOUND_COUNT, _WHATSAPP_DURABLE_JOB_COUNT
    global _WHATSAPP_ENQUEUE_COUNT, _WHATSAPP_RECOVERY_SWEEP_COUNT
    global _WHATSAPP_JOB_AGE, _WHATSAPP_PROCESSING_COUNT
    global _WHATSAPP_SEND_COUNT, _WHATSAPP_SEND_ATTEMPTS

    instruments = (
        _WHATSAPP_INBOUND_COUNT,
        _WHATSAPP_DURABLE_JOB_COUNT,
        _WHATSAPP_ENQUEUE_COUNT,
        _WHATSAPP_RECOVERY_SWEEP_COUNT,
        _WHATSAPP_JOB_AGE,
        _WHATSAPP_PROCESSING_COUNT,
        _WHATSAPP_SEND_COUNT,
        _WHATSAPP_SEND_ATTEMPTS,
    )
    if all(instrument is not None for instrument in instruments):
        return

    with _init_lock:
        meter = metrics.get_meter("copilotgtm.observability")
        if _WHATSAPP_INBOUND_COUNT is None:
            _WHATSAPP_INBOUND_COUNT = meter.create_counter(
                "copilotgtm.whatsapp.inbound.count",
                description="WhatsApp inbound messages by durable intake outcome.",
                unit="{message}",
            )
        if _WHATSAPP_DURABLE_JOB_COUNT is None:
            _WHATSAPP_DURABLE_JOB_COUNT = meter.create_counter(
                "copilotgtm.whatsapp.durable_job.count",
                description="WhatsApp durable job persistence outcomes.",
                unit="{job}",
            )
        if _WHATSAPP_ENQUEUE_COUNT is None:
            _WHATSAPP_ENQUEUE_COUNT = meter.create_counter(
                "copilotgtm.whatsapp.enqueue.count",
                description="WhatsApp initial and recovery queue publication outcomes.",
                unit="{attempt}",
            )
        if _WHATSAPP_RECOVERY_SWEEP_COUNT is None:
            _WHATSAPP_RECOVERY_SWEEP_COUNT = meter.create_counter(
                "copilotgtm.whatsapp.recovery.sweep.count",
                description="WhatsApp database recovery sweeps by outcome.",
                unit="{sweep}",
            )
        if _WHATSAPP_JOB_AGE is None:
            _WHATSAPP_JOB_AGE = meter.create_histogram(
                "copilotgtm.whatsapp.job.age",
                description="Age of WhatsApp jobs encountered by recovery.",
                unit="s",
            )
        if _WHATSAPP_PROCESSING_COUNT is None:
            _WHATSAPP_PROCESSING_COUNT = meter.create_counter(
                "copilotgtm.whatsapp.processing.count",
                description="WhatsApp worker processing outcomes.",
                unit="{job}",
            )
        if _WHATSAPP_SEND_COUNT is None:
            _WHATSAPP_SEND_COUNT = meter.create_counter(
                "copilotgtm.whatsapp.send.count",
                description="Classified WhatsApp provider send outcomes.",
                unit="{attempt}",
            )
        if _WHATSAPP_SEND_ATTEMPTS is None:
            _WHATSAPP_SEND_ATTEMPTS = meter.create_histogram(
                "copilotgtm.whatsapp.send.attempts",
                description="Send attempts required for terminal WhatsApp delivery outcomes.",
                unit="{attempt}",
            )


def _ensure_agent_run_delivery_instruments() -> None:
    """Create delivery instruments lazily under the shared initialization lock."""
    global _AGENT_RUN_DELIVERY_COUNT
    global _AGENT_RUN_DELIVERY_ATTEMPTS, _AGENT_RUN_DELIVERY_LATENCY
    global _AGENT_RUN_DELIVERY_RECOVERY_COUNT, _AGENT_RUN_DELIVERY_RECOVERY_AGE
    if all(
        instrument is not None
        for instrument in (
            _AGENT_RUN_DELIVERY_COUNT,
            _AGENT_RUN_DELIVERY_ATTEMPTS,
            _AGENT_RUN_DELIVERY_LATENCY,
            _AGENT_RUN_DELIVERY_RECOVERY_COUNT,
            _AGENT_RUN_DELIVERY_RECOVERY_AGE,
        )
    ):
        return
    with _init_lock:
        meter = metrics.get_meter("copilotgtm.observability")
        if _AGENT_RUN_DELIVERY_COUNT is None:
            _AGENT_RUN_DELIVERY_COUNT = meter.create_counter(
                "copilotgtm.agent_run.delivery.count",
                description="Agent output delivery outcomes by bounded channel and reason.",
                unit="{delivery}",
            )
        if _AGENT_RUN_DELIVERY_ATTEMPTS is None:
            _AGENT_RUN_DELIVERY_ATTEMPTS = meter.create_histogram(
                "copilotgtm.agent_run.delivery.attempts",
                description="Worker claims used by terminal agent output deliveries.",
                unit="{attempt}",
            )
        if _AGENT_RUN_DELIVERY_LATENCY is None:
            _AGENT_RUN_DELIVERY_LATENCY = meter.create_histogram(
                "copilotgtm.agent_run.delivery.latency",
                description="Time from agent completion to a delivery outcome.",
                unit="ms",
            )
        if _AGENT_RUN_DELIVERY_RECOVERY_COUNT is None:
            _AGENT_RUN_DELIVERY_RECOVERY_COUNT = meter.create_counter(
                "copilotgtm.agent_run.delivery.recovery.count",
                description="Delivery recovery rows by bounded sweep outcome.",
                unit="{row}",
            )
        if _AGENT_RUN_DELIVERY_RECOVERY_AGE is None:
            _AGENT_RUN_DELIVERY_RECOVERY_AGE = meter.create_histogram(
                "copilotgtm.agent_run.delivery.recovery.age",
                description="Age of pending deliveries and stale sending leases.",
                unit="s",
            )


def _status_family(status_code: int) -> str:
    return f"{status_code // 100}xx"


def record_http_server_request(
    *,
    method: str,
    route: str | None,
    status_code: int,
    duration_ms: int,
) -> None:
    """Record one API request against its stable route template."""
    _ensure_http_instruments()
    # Route templates keep labels stable across UUID-heavy REST paths.
    attributes = {
        "http.method": (method or "UNKNOWN").upper(),
        "http.route": route or "unknown",
        "http.response.status_code": str(status_code),
        "http.status_family": _status_family(status_code),
    }
    _HTTP_SERVER_REQUEST_COUNT.add(1, attributes)
    _HTTP_SERVER_REQUEST_DURATION.record(duration_ms, attributes)


def record_celery_task_run(
    *,
    task_name: str | None,
    queue: str | None,
    status: str | None,
    duration_ms: int | None,
) -> None:
    """Record a Celery terminal state and optional execution duration."""
    _ensure_celery_instruments()
    # Queue/task labels are the main drill-down dimensions for worker SLOs and incident triage.
    attributes = {
        "celery.task_name": task_name or "unknown",
        "celery.queue": queue or "unknown",
        "celery.status": (status or "UNKNOWN").upper(),
    }
    _CELERY_TASK_EXECUTION_COUNT.add(1, attributes)
    if duration_ms is not None:
        # Some Celery flows may not produce a start timestamp; count them without fabricating latency.
        _CELERY_TASK_DURATION.record(duration_ms, attributes)


def record_celery_task_memory(
    *,
    task_name: str | None,
    queue: str | None,
    status: str | None,
    rss_mb: float,
    rss_delta_mb: float | None,
) -> None:
    """Record worker RSS without task arguments or tenant identifiers."""
    _ensure_celery_instruments()
    attributes = {
        "celery.task_name": task_name or "unknown",
        "celery.queue": queue or "unknown",
        "celery.status": (status or "UNKNOWN").upper(),
    }
    _CELERY_TASK_RSS_MB.record(rss_mb, attributes)
    if rss_delta_mb is not None:
        _CELERY_TASK_RSS_DELTA_MB.record(rss_delta_mb, attributes)


def _bounded(value: str, allowed: frozenset[str]) -> str:
    return value if value in allowed else "unknown"


def record_whatsapp_inbound(outcome: str) -> None:
    """Count a webhook intake outcome without provider or user identifiers."""
    _ensure_whatsapp_instruments()
    _WHATSAPP_INBOUND_COUNT.add(
        1,
        {"outcome": _bounded(outcome, _WHATSAPP_INBOUND_OUTCOMES)},
    )


def record_whatsapp_durable_job(outcome: str) -> None:
    """Count durable WhatsApp job persistence outcomes."""
    _ensure_whatsapp_instruments()
    _WHATSAPP_DURABLE_JOB_COUNT.add(
        1,
        {"outcome": _bounded(outcome, _WHATSAPP_DURABLE_JOB_OUTCOMES)},
    )


def record_whatsapp_enqueue(outcome: str) -> None:
    """Count initial and recovery broker publication outcomes."""
    _ensure_whatsapp_instruments()
    _WHATSAPP_ENQUEUE_COUNT.add(
        1,
        {"outcome": _bounded(outcome, _WHATSAPP_ENQUEUE_OUTCOMES)},
    )


def record_whatsapp_recovery_sweep(outcome: str) -> None:
    """Count terminal outcomes of the scheduled WhatsApp recovery sweep."""
    _ensure_whatsapp_instruments()
    _WHATSAPP_RECOVERY_SWEEP_COUNT.add(
        1,
        {"outcome": _bounded(outcome, _WHATSAPP_RECOVERY_OUTCOMES)},
    )


def record_whatsapp_job_age(status: str, age_seconds: float) -> None:
    """Observe recoverable job age using only the bounded status dimension."""
    _ensure_whatsapp_instruments()
    _WHATSAPP_JOB_AGE.record(
        max(age_seconds, 0.0),
        {"status": _bounded(status, _WHATSAPP_JOB_STATUSES)},
    )


def record_whatsapp_processing(outcome: str) -> None:
    """Count worker processing outcomes independently from provider delivery."""
    _ensure_whatsapp_instruments()
    _WHATSAPP_PROCESSING_COUNT.add(
        1,
        {"outcome": _bounded(outcome, _WHATSAPP_PROCESSING_OUTCOMES)},
    )


def record_whatsapp_send(outcome: str, *, attempts: int | None = None) -> None:
    """Record the classified Meta outcome and optional terminal attempt count."""
    _ensure_whatsapp_instruments()
    bounded_outcome = _bounded(outcome, _WHATSAPP_SEND_OUTCOMES)
    _WHATSAPP_SEND_COUNT.add(1, {"outcome": bounded_outcome})
    if attempts is not None:
        _WHATSAPP_SEND_ATTEMPTS.record(
            max(attempts, 0),
            {"outcome": "sent" if bounded_outcome == "sent" else "failed"},
        )


def record_agent_run_delivery(
    outcome: str,
    *,
    provider: str | None = None,
    reason: str | None = None,
    attempts: int | None = None,
    latency_ms: int | None = None,
) -> None:
    """Record one delivery outcome without tenant, run, mailbox, or recipient labels."""

    _ensure_agent_run_delivery_instruments()
    attributes = {
        "channel": "email",
        "outcome": _bounded(outcome, _AGENT_RUN_DELIVERY_OUTCOMES),
        "provider": _bounded(provider or "unknown", _AGENT_RUN_DELIVERY_PROVIDERS),
        "reason": _bounded(reason or "none", _AGENT_RUN_DELIVERY_REASONS),
    }
    _AGENT_RUN_DELIVERY_COUNT.add(1, attributes)
    if attempts is not None:
        _AGENT_RUN_DELIVERY_ATTEMPTS.record(max(attempts, 0), attributes)
    if latency_ms is not None:
        _AGENT_RUN_DELIVERY_LATENCY.record(max(latency_ms, 0), attributes)


def record_agent_run_delivery_recovery(outcome: str, *, rows: int = 1) -> None:
    """Count recovery rows without tenant, run, or mailbox identifiers."""

    _ensure_agent_run_delivery_instruments()
    _AGENT_RUN_DELIVERY_RECOVERY_COUNT.add(
        max(rows, 0),
        {"outcome": _bounded(outcome, _AGENT_RUN_DELIVERY_RECOVERY_OUTCOMES)},
    )


def record_agent_run_delivery_recovery_age(status: str, age_seconds: float) -> None:
    """Observe pending-row or stale-lease age with a bounded status label."""

    _ensure_agent_run_delivery_instruments()
    _AGENT_RUN_DELIVERY_RECOVERY_AGE.record(
        max(age_seconds, 0.0),
        {"status": _bounded(status, _AGENT_RUN_DELIVERY_RECOVERY_STATUSES)},
    )


__all__ = [
    "record_agent_run_delivery",
    "record_agent_run_delivery_recovery",
    "record_agent_run_delivery_recovery_age",
    "record_celery_task_memory",
    "record_celery_task_run",
    "record_http_server_request",
    "record_whatsapp_durable_job",
    "record_whatsapp_enqueue",
    "record_whatsapp_inbound",
    "record_whatsapp_job_age",
    "record_whatsapp_processing",
    "record_whatsapp_recovery_sweep",
    "record_whatsapp_send",
]
