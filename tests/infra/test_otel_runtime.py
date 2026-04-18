import asyncio
import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import reload

import pytest
import src.api.fastapi_app as fastapi_app
import src.infra.observability.sentry as sentry_runtime
import src.infra.utils.app_state as app_state
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Client as HttpxClient
from src.api.middleware.request_context import RequestContextMiddleware
from src.infra.config.configs import otel_config, sentry_config
from src.infra.utils.logger import configure_logging, get_logger, resolve_celery_component
from src.workers.base import celery_base
from supabase import Client as SupabaseClient
from supabase import create_client as create_supabase_client
from supabase.lib.client_options import SyncClientOptions


@dataclass
class _DummyWorkerConfig:
    broker_url: str = "redis://localhost:6379/0"
    backend_url: str = "redis://localhost:6379/0"
    ssl_config: dict | None = None


def _structured_lines(stderr: str) -> list[dict]:
    return [json.loads(line) for line in stderr.splitlines() if line.strip().startswith("{")]


def _build_supabase_base_client() -> tuple[SupabaseClient, HttpxClient]:
    transport = HttpxClient(verify=True, follow_redirects=True, http2=True)
    client = create_supabase_client(
        "https://example.supabase.co",
        "anon-key",
        options=SyncClientOptions(httpx_client=transport),
    )
    return client, transport


def test_configure_logging_formats_stdlib_loggers_as_json(capsys, monkeypatch):
    monkeypatch.setenv("ENV", "dev")
    configure_logging(component="api", force=True)

    logger = logging.getLogger("stdlib_logger")
    logger.info("raw logger message", extra={"event": "test_event", "custom_field": "value"})

    payload = _structured_lines(capsys.readouterr().err)[-1]

    assert payload["logger"] == "stdlib_logger"
    assert payload["message"] == "raw logger message"
    assert payload["component"] == "api"
    assert payload["event"] == "test_event"
    assert payload["custom_field"] == "value"


def test_configure_logging_serializes_non_json_extra_values(capsys, monkeypatch):
    monkeypatch.setenv("ENV", "dev")
    configure_logging(component="worker", force=True)

    logger = logging.getLogger("stdlib_logger")
    logger.error("raw logger message", extra={"event": "test_event", "bad_value": object()})

    payload = _structured_lines(capsys.readouterr().err)[-1]

    assert payload["logger"] == "stdlib_logger"
    assert payload["event"] == "test_event"
    assert isinstance(payload["bad_value"], str)


def test_request_context_access_logs_include_structured_http_fields(capsys, monkeypatch):
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("ACCESS_LOG_LEVEL", "INFO")
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


def test_request_context_logs_success_requests_in_dev_by_default(capsys, monkeypatch):
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.delenv("ACCESS_LOG_LEVEL", raising=False)
    configure_logging(component="api", force=True)
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/ping", headers={"X-Request-Id": "req-suppressed"})

    assert response.status_code == 200
    payload = next(
        item
        for item in _structured_lines(capsys.readouterr().err)
        if item.get("event") == "http_request"
    )

    assert payload["level"] == "INFO"
    assert payload["request_id"] == "req-suppressed"


def test_request_context_suppresses_success_logs_in_prod_by_default(capsys, monkeypatch):
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.delenv("ACCESS_LOG_LEVEL", raising=False)
    configure_logging(component="api", force=True)
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/ping", headers={"X-Request-Id": "req-prod-suppressed"})

    assert response.status_code == 200
    payloads = _structured_lines(capsys.readouterr().err)
    assert not any(item.get("event") == "http_request" for item in payloads)


def test_request_context_honors_access_log_level_override_when_root_is_warning(capsys, monkeypatch):
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("ACCESS_LOG_LEVEL", "INFO")
    configure_logging(component="api", force=True)
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/ping", headers={"X-Request-Id": "req-access-override"})

    assert response.status_code == 200

    payload = next(
        item
        for item in _structured_lines(capsys.readouterr().err)
        if item.get("event") == "http_request"
    )

    assert payload["level"] == "INFO"
    assert payload["request_id"] == "req-access-override"
    assert payload["http_status_code"] == 200


def test_request_context_logs_4xx_requests_as_warning(capsys, monkeypatch):
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("ACCESS_LOG_LEVEL", "INFO")
    configure_logging(component="api", force=True)
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    with TestClient(app) as client:
        response = client.get("/missing", headers={"X-Request-Id": "req-404"})

    assert response.status_code == 404

    payload = next(
        item
        for item in _structured_lines(capsys.readouterr().err)
        if item.get("event") == "http_request"
    )

    assert payload["level"] == "WARNING"
    assert payload["http_status_code"] == 404
    assert payload["request_id"] == "req-404"


def test_request_context_logs_5xx_requests_as_error(capsys, monkeypatch):
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("ACCESS_LOG_LEVEL", "INFO")
    configure_logging(component="api", force=True)
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/boom")
    def boom():
        raise RuntimeError("boom")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom", headers={"X-Request-Id": "req-500"})

    assert response.status_code == 500

    payload = next(
        item
        for item in _structured_lines(capsys.readouterr().err)
        if item.get("event") == "http_request"
    )

    assert payload["level"] == "ERROR"
    assert payload["http_status_code"] == 500
    assert payload["request_id"] == "req-500"


def test_request_context_logs_health_paths_at_debug(capsys, monkeypatch):
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("ACCESS_LOG_LEVEL", "INFO")
    configure_logging(component="api", force=True)
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/health")
    def health():
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/health", headers={"X-Request-Id": "req-health"})

    assert response.status_code == 200

    payload = next(
        item
        for item in _structured_lines(capsys.readouterr().err)
        if item.get("event") == "http_request"
    )

    assert payload["level"] == "DEBUG"
    assert payload["http_path"] == "/health"
    assert payload["request_id"] == "req-health"


def test_configure_logging_renders_console_extra_fields_inline(capsys, monkeypatch):
    monkeypatch.setenv("ENV", "local")
    configure_logging(component="api", force=True)

    logger = logging.getLogger("api.access")
    logger.info(
        "HTTP request completed",
        extra={
            "event": "http_request",
            "http_method": "GET",
            "http_path": "/ping",
            "http_status_code": 200,
            "duration_ms": 42,
            "organisation_id": "org-123",
        },
    )

    rendered = capsys.readouterr().err

    assert "HTTP request completed" in rendered
    assert "method=GET" in rendered
    assert "path=/ping" in rendered
    assert "status=200" in rendered
    assert "duration_ms=42" in rendered
    assert "organisation_id=org-123" in rendered
    assert "event=http_request" not in rendered


def test_configure_logging_renders_uvicorn_logger_name_without_color_message(capsys, monkeypatch):
    monkeypatch.setenv("ENV", "local")
    configure_logging(component="api", force=True)

    logger = logging.getLogger("uvicorn.error")
    logger.info(
        "Started server process [9]",
        extra={"color_message": "Started server process [%d]"},
    )

    rendered = capsys.readouterr().err

    assert "[uvicorn.error]" in rendered
    assert "color_message=" not in rendered
    assert "Started server process [9]" in rendered


def test_create_fastapi_app_instruments_shared_app_instance(mocker):
    mocker.patch.object(otel_config, "OTEL_EXPORTER_OTLP_ENDPOINT", "")

    reload(app_state)
    reload(fastapi_app)

    instrument_mock = mocker.patch("src.api.fastapi_app.instrument_fastapi_app")

    app = fastapi_app.create_fastapi_app()

    assert app is app_state.fastapp
    instrument_mock.assert_called_once_with(app_state.fastapp)


def test_fastapi_lifespan_initializes_sentry_after_supabase_clients(mocker):
    call_order: list[str] = []

    mocker.patch("src.api.fastapi_app.initialize_redis_pool", side_effect=lambda: None)
    mocker.patch("src.api.fastapi_app.initialize_async_redis_pool", side_effect=lambda: None)
    mocker.patch("src.api.fastapi_app.async_init_otel", side_effect=lambda **kwargs: None)
    mocker.patch("src.api.fastapi_app.close_redis_pool", side_effect=lambda: None)
    mocker.patch("src.api.fastapi_app.close_async_redis_pool", side_effect=lambda: None)
    mocker.patch("src.api.fastapi_app.init_sentry", side_effect=lambda: call_order.append("sentry"))

    async def _admin_init():
        call_order.append("admin")

    async def _user_init():
        call_order.append("user")

    async def _token_init():
        call_order.append("token")

    mocker.patch(
        "src.infra.auth.supabase_admin_client.supabase_admin_client.initialize",
        side_effect=_admin_init,
    )
    mocker.patch(
        "src.infra.auth.supabase_user_client.supabase_user_client.initialize",
        side_effect=_user_init,
    )
    mocker.patch(
        "src.infra.security.token_revocation.token_revocation_service.initialize",
        side_effect=_token_init,
    )

    async def _run() -> None:
        async with fastapi_app.lifespan(FastAPI()):
            return None

    asyncio.run(_run())

    assert call_order == ["admin", "user", "token", "sentry"]


def test_create_celery_app_defers_otel_init_until_worker_process_start(mocker):
    init_mock = mocker.patch("src.infra.observability.otel.init_otel")
    mocker.patch("src.infra.utils.async_runner._clear_async_db_provider_cache")
    mocker.patch("src.infra.utils.async_runner._clear_sync_db_provider_cache")

    app = celery_base.create_celery_app("unit_worker", _DummyWorkerConfig(), "tests.fake_tasks")
    app.set_current()

    init_mock.assert_not_called()

    celery_base.worker_process_init.send(sender=None)

    init_mock.assert_any_call(service_name_suffix="-unit_worker")


def test_create_celery_app_uses_current_app_name_when_multiple_apps_exist(mocker):
    init_mock = mocker.patch("src.infra.observability.otel.init_otel")
    mocker.patch("src.infra.utils.async_runner._clear_async_db_provider_cache")
    mocker.patch("src.infra.utils.async_runner._clear_sync_db_provider_cache")

    chat_app = celery_base.create_celery_app(
        "chat_worker", _DummyWorkerConfig(), "tests.chat_tasks"
    )
    celery_base.create_celery_app("deal_worker", _DummyWorkerConfig(), "tests.deal_tasks")
    chat_app.set_current()

    celery_base.worker_process_init.send(sender=None)

    assert init_mock.call_args_list[-1].kwargs["service_name_suffix"] == "-chat_worker"


def test_create_celery_app_uses_beat_suffix_for_beat_init(mocker):
    init_mock = mocker.patch("src.infra.observability.otel.init_otel")
    app = celery_base.create_celery_app("chat_worker", _DummyWorkerConfig(), "tests.chat_tasks")
    sender = type("BeatSender", (), {"app": app})()

    mocker.patch("src.infra.utils.logger.sys.argv", ["celery", "-A", "x", "beat"])

    celery_base.beat_init.send(sender=sender)

    init_mock.assert_any_call(service_name_suffix="-chat_beat")


def test_create_celery_app_sets_resilient_redis_transport_options():
    app = celery_base.create_celery_app("unit_worker", _DummyWorkerConfig(), "tests.fake_tasks")

    assert app.conf.broker_transport_options == {
        "socket_timeout": 30,
        "socket_connect_timeout": 30,
        "socket_keepalive": True,
        "retry_on_timeout": True,
        "health_check_interval": 30,
    }


def test_otel_log_sanitizer_drops_source_location_fields():
    from src.infra.observability.otel import _OtelLogSanitizer

    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname="/app/server.py",
        lineno=84,
        msg="Started server process [8]",
        args=(),
        exc_info=None,
        func="_serve",
    )
    record.color_message = "Started server process [%d]"

    allowed = _OtelLogSanitizer().filter(record)

    assert allowed is True
    assert record.pathname == ""
    assert record.filename == ""
    assert record.module == ""
    assert record.funcName == ""
    assert record.lineno == 0
    assert record.color_message == ""


def test_init_otel_is_noop_when_disabled(mocker):
    import src.infra.observability.otel as otel_runtime

    mocker.patch.object(otel_runtime, "_otel_initialized", False)
    mocker.patch.object(otel_config, "OTEL_ENABLED", False)
    mocker.patch.object(otel_config, "OTEL_EXPORTER_OTLP_ENDPOINT", "https://example.com/otlp")

    setup_tracing = mocker.patch("src.infra.observability.otel._setup_tracing")
    setup_logging = mocker.patch("src.infra.observability.otel._setup_logging")
    auto_instrument = mocker.patch("src.infra.observability.otel._auto_instrument")

    otel_runtime.init_otel()

    setup_tracing.assert_not_called()
    setup_logging.assert_not_called()
    auto_instrument.assert_not_called()
    assert otel_runtime._otel_initialized is False


def test_async_init_otel_primes_supabase_credentials_before_instrumentation(mocker, monkeypatch):
    import src.infra.observability.otel as otel_runtime
    from src.infra.config.configs import supabase_config

    call_order: list[str] = []
    monkeypatch.setenv("ENV", "dev")

    async def _load_headers():
        call_order.append("headers")

    async def _load_supabase():
        call_order.append("supabase")
        return ("https://example.supabase.co", "anon", "service", "jwt")

    mocker.patch.object(otel_runtime, "_otel_initialized", False)
    mocker.patch.object(otel_config, "OTEL_ENABLED", True)
    mocker.patch.object(otel_config, "OTEL_EXPORTER_OTLP_ENDPOINT", "https://example.com/otlp")
    mocker.patch.object(otel_config, "load_headers", side_effect=_load_headers)
    mocker.patch.object(supabase_config, "SUPABASE_URL", "")
    mocker.patch.object(supabase_config, "get_credentials", side_effect=_load_supabase)
    mocker.patch("src.infra.observability.otel._setup_tracing", side_effect=lambda *args: None)
    mocker.patch("src.infra.observability.otel._setup_logging", side_effect=lambda *args: None)
    mocker.patch(
        "src.infra.observability.otel._auto_instrument",
        side_effect=lambda: call_order.append("instrument"),
    )

    asyncio.run(otel_runtime.async_init_otel())

    assert call_order == ["headers", "supabase", "instrument"]


def test_instrument_httpx_skips_local_runtime(mocker, monkeypatch):
    import src.infra.observability.otel as otel_runtime

    monkeypatch.setenv("ENV", "local")
    monkeypatch.delenv("OTEL_PYTHON_HTTPX_EXCLUDED_URLS", raising=False)
    mocker.patch.object(otel_config, "OTEL_ENABLED", True)
    mocker.patch.object(otel_config, "OTEL_EXPORTER_OTLP_ENDPOINT", "https://example.com/otlp")

    instrument_mock = mocker.patch(
        "opentelemetry.instrumentation.httpx.HTTPXClientInstrumentor.instrument"
    )

    otel_runtime._instrument_httpx()

    instrument_mock.assert_not_called()
    assert "OTEL_PYTHON_HTTPX_EXCLUDED_URLS" not in os.environ


def test_instrument_httpx_applies_supabase_exclusion_for_non_local_runtime(mocker, monkeypatch):
    import src.infra.observability.otel as otel_runtime
    from src.infra.config.configs import supabase_config

    monkeypatch.setenv("ENV", "dev")
    monkeypatch.delenv("OTEL_PYTHON_HTTPX_EXCLUDED_URLS", raising=False)
    mocker.patch.object(supabase_config, "SUPABASE_URL", "https://example.supabase.co/")

    instrument_mock = mocker.patch(
        "opentelemetry.instrumentation.httpx.HTTPXClientInstrumentor.instrument"
    )

    otel_runtime._instrument_httpx()

    instrument_mock.assert_called_once()
    assert os.environ["OTEL_PYTHON_HTTPX_EXCLUDED_URLS"] == "https://example.supabase.co"


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


def test_init_sentry_is_noop_when_dsn_missing(mocker):
    mocker.patch.object(sentry_runtime, "_sentry_initialized", False)
    mocker.patch.object(sentry_config, "SENTRY_DSN", "")

    init_mock = mocker.patch("sentry_sdk.init")

    sentry_runtime.init_sentry()

    init_mock.assert_not_called()
    assert sentry_runtime._sentry_initialized is False


def test_init_sentry_initializes_sdk_with_expected_options(mocker):
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.fastapi import FastApiIntegration

    mocker.patch.object(sentry_runtime, "_sentry_initialized", False)
    mocker.patch.object(sentry_config, "SENTRY_DSN", "https://example@sentry.io/123")
    mocker.patch.object(sentry_config, "SENTRY_ENVIRONMENT", "prod")
    mocker.patch.object(sentry_config, "SENTRY_SEND_DEFAULT_PII", True)
    mocker.patch.object(sentry_config, "SENTRY_TRACES_SAMPLE_RATE", 0.35)

    init_mock = mocker.patch("sentry_sdk.init")

    sentry_runtime.init_sentry()

    init_mock.assert_called_once()
    kwargs = init_mock.call_args.kwargs
    assert kwargs["dsn"] == "https://example@sentry.io/123"
    assert kwargs["environment"] == "prod"
    assert kwargs["send_default_pii"] is True
    assert kwargs["traces_sample_rate"] == 0.35
    assert any(isinstance(item, FastApiIntegration) for item in kwargs["integrations"])
    assert any(isinstance(item, CeleryIntegration) for item in kwargs["integrations"])
    assert sentry_runtime._sentry_initialized is True


def test_init_sentry_is_idempotent(mocker):
    mocker.patch.object(sentry_runtime, "_sentry_initialized", False)
    mocker.patch.object(sentry_config, "SENTRY_DSN", "https://example@sentry.io/123")

    init_mock = mocker.patch("sentry_sdk.init")

    sentry_runtime.init_sentry()
    sentry_runtime.init_sentry()

    init_mock.assert_called_once()


def test_sentry_runtime_does_not_import_sdk_at_module_load():
    assert "sentry_sdk" not in sentry_runtime.__dict__


def test_configure_logging_suppresses_celery_internal_loggers():
    configure_logging(component="worker", force=True)

    for logger_name in [
        "celery.worker.consumer.connection",
        "celery.worker.consumer.mingle",
        "celery.worker.consumer.control",
        "celery.worker.control",
        "celery.apps.worker",
        "kombu.connection",
        "kombu.pidbox",
    ]:
        assert logging.getLogger(logger_name).level == logging.WARNING


def test_redis_connection_pool_success_logs_are_debug(capsys, monkeypatch):
    import src.infra.messaging.redis_client as redis_client_runtime

    class _DummyConfig:
        env = "local"

        @property
        def get_redis_client_details(self):
            return ("redis", 6379, "")

    class _DummyRedisClient:
        def __init__(self, connection_pool=None):
            self.connection_pool = connection_pool

        def ping(self):
            return True

    monkeypatch.setenv("ENV", "local")
    configure_logging(component="api", force=True)
    monkeypatch.setattr(redis_client_runtime, "Config", lambda: _DummyConfig())
    monkeypatch.setattr(
        redis_client_runtime.redis,
        "ConnectionPool",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(redis_client_runtime.redis, "Redis", _DummyRedisClient)

    manager = redis_client_runtime.RedisConnectionManager()
    manager._sync_pool = None
    manager._initialized = False

    manager._initialize_pool()

    rendered = capsys.readouterr().err
    assert "Redis connection pool initialized successfully" not in rendered


def test_nango_client_lifecycle_logs_are_debug(capsys, monkeypatch):
    nango_runtime = pytest.importorskip("src.service.integrations.nango_client")

    monkeypatch.setenv("ENV", "local")
    configure_logging(component="api", force=True)
    monkeypatch.setattr(nango_runtime.nango_integration_config, "NANGO_SECRET_KEY", "secret")
    monkeypatch.setattr(
        nango_runtime.nango_integration_config,
        "NANGO_BASE_URL",
        "https://api.nango.dev",
    )

    client = nango_runtime.NangoClient()
    asyncio.run(client.close())

    rendered = capsys.readouterr().err
    assert "Initialized Nango REST API client" not in rendered
    assert "Closed Nango REST API client" not in rendered


def test_azure_key_vault_client_creation_logs_are_debug(capsys, monkeypatch):
    import src.infra.secrets.azure_key_vault as key_vault_runtime

    class _FakeCredential:
        async def close(self):
            return None

    class _FakeSecretClient:
        def __init__(self, vault_url, credential):
            self.vault_url = vault_url
            self.credential = credential

        async def close(self):
            return None

    monkeypatch.setenv("ENV", "local")
    configure_logging(component="worker", force=True)
    monkeypatch.setattr(
        key_vault_runtime,
        "DefaultAzureCredential",
        lambda **kwargs: _FakeCredential(),
    )
    monkeypatch.setattr(key_vault_runtime, "SecretClient", _FakeSecretClient)

    provider = key_vault_runtime.AzureKeyVaultSecretProvider(
        vault_url="https://example.vault.azure.net/"
    )

    asyncio.run(provider._create_client())

    rendered = capsys.readouterr().err
    assert "Auth strategy (secrets):" not in rendered
    assert "Azure Key Vault client created successfully" not in rendered

    asyncio.run(provider.close())


def test_verify_token_logs_auth_failure_event(capsys, monkeypatch):
    import src.infra.auth.supabase_user_client as supabase_runtime

    monkeypatch.setenv("ENV", "dev")
    configure_logging(component="api", force=True)

    client = supabase_runtime.SupabaseUserClient.__new__(supabase_runtime.SupabaseUserClient)

    with pytest.raises(supabase_runtime.SupabaseAuthError):
        client.verify_token("not-a-jwt")

    payload = _structured_lines(capsys.readouterr().err)[-1]

    assert payload["message"] == "Token verification failed"
    assert payload["event"] == "auth_failure"
    assert payload["auth_operation"] == "verify_token"


def test_supabase_user_client_sign_in_uses_startup_httpx_transport(mocker):
    import src.infra.auth.supabase_user_client as supabase_runtime

    request_client = mocker.Mock()
    request_client.auth.sign_in_with_password.return_value.model_dump.return_value = {
        "session": {"access_token": "token"}
    }
    create_client_mock = mocker.patch.object(
        supabase_runtime, "create_client", return_value=request_client
    )

    base_client, transport = _build_supabase_base_client()
    try:
        client = supabase_runtime.SupabaseUserClient()
        client.supabase_url = "https://example.supabase.co"
        client.supabase_anon_key = "anon-key"
        client.client = base_client
        client._auth_httpx_client = transport

        result = client.sign_in_with_password("user@example.com", "password123")

        create_client_mock.assert_called_once()
        options = create_client_mock.call_args.kwargs["options"]
        assert options.httpx_client is transport
        assert options.headers == base_client.options.headers
        assert options.headers is not base_client.options.headers
        request_client.auth.sign_in_with_password.assert_called_once()
        assert result == {"session": {"access_token": "token"}}
    finally:
        transport.close()


def test_supabase_user_client_verify_token_uses_startup_httpx_transport(mocker):
    import src.infra.auth.supabase_user_client as supabase_runtime

    request_client = mocker.Mock()
    request_client.auth.get_user.return_value.model_dump.return_value = {"user": {"id": "user-1"}}
    create_client_mock = mocker.patch.object(
        supabase_runtime, "create_client", return_value=request_client
    )

    base_client, transport = _build_supabase_base_client()
    try:
        client = supabase_runtime.SupabaseUserClient()
        client.supabase_url = "https://example.supabase.co"
        client.supabase_anon_key = "anon-key"
        client.client = base_client
        client._auth_httpx_client = transport

        result = client.verify_token("header.payload.signature")

        create_client_mock.assert_called_once()
        options = create_client_mock.call_args.kwargs["options"]
        assert options.httpx_client is transport
        request_client.auth.get_user.assert_called_once_with("header.payload.signature")
        assert result == {"user": {"id": "user-1"}}
    finally:
        transport.close()


def test_supabase_user_client_refresh_access_token_uses_startup_httpx_transport(mocker):
    import src.infra.auth.supabase_user_client as supabase_runtime

    request_client = mocker.Mock()
    request_client.auth.refresh_session.return_value.model_dump.return_value = {
        "session": {"access_token": "token", "refresh_token": "refresh"}
    }
    create_client_mock = mocker.patch.object(
        supabase_runtime, "create_client", return_value=request_client
    )

    base_client, transport = _build_supabase_base_client()
    try:
        client = supabase_runtime.SupabaseUserClient()
        client.supabase_url = "https://example.supabase.co"
        client.supabase_anon_key = "anon-key"
        client.client = base_client
        client._auth_httpx_client = transport

        result = client.refresh_access_token("refresh-token")

        create_client_mock.assert_called_once()
        options = create_client_mock.call_args.kwargs["options"]
        assert options.httpx_client is transport
        request_client.auth.refresh_session.assert_called_once_with("refresh-token")
        assert result == {"session": {"access_token": "token", "refresh_token": "refresh"}}
    finally:
        transport.close()


def test_secrets_manager_logs_secret_fetch_failure_events(capsys, monkeypatch):
    import src.infra.secrets.manager as secrets_manager_runtime

    class _FailingProvider:
        async def get(self, name: str):
            raise RuntimeError(f"boom:{name}")

        async def get_many(self, names: Iterable[str]) -> dict[str, str | None]:
            return dict.fromkeys(names)

    monkeypatch.setenv("ENV", "dev")
    configure_logging(component="api", force=True)

    manager = secrets_manager_runtime.SecretsManager(
        provider=_FailingProvider(),
        max_retries=1,
        initial_retry_delay=0,
        max_retry_delay=0,
    )

    assert asyncio.run(manager.get("tenant-secret")) is None

    payloads = _structured_lines(capsys.readouterr().err)
    retry_payload = next(item for item in payloads if item.get("event") == "secret_fetch_retry")
    failure_payload = next(item for item in payloads if item.get("event") == "secret_fetch_failed")

    assert retry_payload["secret_name"] == "tenant-secret"
    assert failure_payload["secret_name"] == "tenant-secret"
    assert failure_payload["message"] == "Secret fetch failed after retries"


def test_nango_connect_session_logs_structured_fields(capsys, monkeypatch):
    nango_runtime = pytest.importorskip("src.service.integrations.nango_client")

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"token": "session-token", "expires_at": "soon"}

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url, json):
            return _FakeResponse()

        async def aclose(self):
            return None

    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    configure_logging(component="api", force=True)
    monkeypatch.setattr(nango_runtime.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        nango_runtime.nango_integration_config, "NANGO_BASE_URL", "https://api.nango.dev"
    )
    monkeypatch.setattr(nango_runtime.nango_integration_config, "NANGO_SECRET_KEY", "secret")

    client = nango_runtime.NangoClient()
    result = asyncio.run(
        client.create_connect_session(
            end_user_id="user-123",
            organization_id="org-123",
            integration_id="hubspot",
        )
    )

    assert result["token"] == "session-token"
    payload = next(
        item
        for item in _structured_lines(capsys.readouterr().err)
        if item.get("event") == "integration_connect_session_created"
    )

    assert payload["message"] == "Connect session created"
    assert payload["organisation_id"] == "org-123"
    assert payload["user_id"] == "user-123"
    assert payload["integration_id"] == "hubspot"

    asyncio.run(client.close())
