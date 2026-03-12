import json
import logging
from dataclasses import dataclass
from importlib import reload

import src.api.fastapi_app as fastapi_app
import src.infra.observability.sentry as sentry_runtime
import src.infra.utils.app_state as app_state
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.api.middleware.request_context import RequestContextMiddleware
from src.infra.config.configs import otel_config, sentry_config
from src.infra.utils.logger import configure_logging, get_logger, resolve_celery_component
from src.workers.base import celery_base


@dataclass
class _DummyWorkerConfig:
    broker_url: str = "redis://localhost:6379/0"
    backend_url: str = "redis://localhost:6379/0"
    ssl_config: dict | None = None


def _structured_lines(stderr: str) -> list[dict]:
    return [json.loads(line) for line in stderr.splitlines() if line.strip().startswith("{")]


def test_configure_logging_formats_stdlib_loggers_as_json(capsys):
    configure_logging(component="api", force=True)

    logger = logging.getLogger("stdlib_logger")
    logger.info("raw logger message", extra={"event": "test_event", "custom_field": "value"})

    payload = _structured_lines(capsys.readouterr().err)[-1]

    assert payload["logger"] == "stdlib_logger"
    assert payload["message"] == "raw logger message"
    assert payload["component"] == "api"
    assert payload["event"] == "test_event"
    assert payload["custom_field"] == "value"


def test_configure_logging_serializes_non_json_extra_values(capsys):
    configure_logging(component="worker", force=True)

    logger = logging.getLogger("stdlib_logger")
    logger.error("raw logger message", extra={"event": "test_event", "bad_value": object()})

    payload = _structured_lines(capsys.readouterr().err)[-1]

    assert payload["logger"] == "stdlib_logger"
    assert payload["event"] == "test_event"
    assert isinstance(payload["bad_value"], str)


def test_request_context_access_logs_include_structured_http_fields(capsys):
    configure_logging(component="api", force=True)
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/ping", headers={"X-Request-Id": "req-123"})

    assert response.status_code == 200

    payload = next(
        item
        for item in _structured_lines(capsys.readouterr().err)
        if item.get("event") == "http_request"
    )

    assert payload["message"] == "HTTP request completed"
    assert payload["http_method"] == "GET"
    assert payload["http_path"] == "/ping"
    assert payload["http_status_code"] == 200
    assert payload["request_id"] == "req-123"
    assert "duration_ms" in payload


def test_create_fastapi_app_instruments_shared_app_instance(mocker):
    mocker.patch.object(otel_config, "OTEL_EXPORTER_OTLP_ENDPOINT", "")

    reload(app_state)
    reload(fastapi_app)

    instrument_mock = mocker.patch("src.api.fastapi_app.instrument_fastapi_app")

    app = fastapi_app.create_fastapi_app()

    assert app is app_state.fastapp
    instrument_mock.assert_called_once_with(app_state.fastapp)


def test_create_celery_app_defers_otel_init_until_worker_process_start(mocker):
    init_mock = mocker.patch("src.infra.observability.otel.init_otel")
    mocker.patch("src.infra.utils.async_runner._clear_async_db_provider_cache")
    mocker.patch("src.infra.utils.async_runner._clear_sync_db_provider_cache")

    celery_base.create_celery_app("unit_worker", _DummyWorkerConfig(), "tests.fake_tasks")

    init_mock.assert_not_called()

    celery_base.worker_process_init.send(sender=None)

    init_mock.assert_any_call(service_name_suffix="-unit_worker")


def test_get_logger_uses_root_pipeline_without_local_console_handlers():
    configure_logging(component="api", force=True)

    logger = get_logger("app_logger")

    assert logger.propagate is True
    assert not any(isinstance(handler, logging.StreamHandler) for handler in logger.handlers)


def test_get_logger_without_name_uses_caller_module():
    configure_logging(component="api", force=True)

    logger = get_logger()

    assert logger.name == __name__


def test_configure_logging_suppresses_http_client_noise():
    configure_logging(component="api", force=True)

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_configure_logging_normalizes_uvicorn_loggers():
    configure_logging(component="api", force=True)

    assert logging.getLogger("uvicorn.error").propagate is True
    assert logging.getLogger("uvicorn.error").handlers == []
    assert logging.getLogger("uvicorn.access").propagate is True
    assert logging.getLogger("uvicorn.access").level == logging.WARNING


def test_resolve_celery_component_distinguishes_beat_from_worker(mocker):
    mocker.patch("src.infra.utils.logger.sys.argv", ["celery", "-A", "x", "beat"])
    assert resolve_celery_component("chat_worker") == "chat_beat"

    mocker.patch("src.infra.utils.logger.sys.argv", ["celery", "-A", "x", "worker"])
    assert resolve_celery_component("chat_worker") == "chat_worker"


def test_init_sentry_is_noop_even_with_dsn_configured(mocker):
    mocker.patch.object(sentry_runtime, "_sentry_initialized", False)
    mocker.patch.object(sentry_config, "SENTRY_DSN", "https://example@sentry.io/123")

    init_mock = mocker.patch("sentry_sdk.init")

    sentry_runtime.init_sentry()

    init_mock.assert_not_called()
    assert sentry_runtime._sentry_initialized is True
