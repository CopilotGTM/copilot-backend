import asyncio
import json
import logging
from dataclasses import dataclass
from importlib import reload

import pytest
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


def test_request_context_suppresses_success_logs_in_dev_by_default(capsys, monkeypatch):
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


def test_create_celery_app_defers_otel_init_until_worker_process_start(mocker):
    init_mock = mocker.patch("src.infra.observability.otel.init_otel")
    mocker.patch("src.infra.utils.async_runner._clear_async_db_provider_cache")
    mocker.patch("src.infra.utils.async_runner._clear_sync_db_provider_cache")

    celery_base.create_celery_app("unit_worker", _DummyWorkerConfig(), "tests.fake_tasks")

    init_mock.assert_not_called()

    celery_base.worker_process_init.send(sender=None)

    init_mock.assert_any_call(service_name_suffix="-unit_worker")


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
    import src.service.integrations.nango_client as nango_runtime

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


def test_secrets_manager_logs_secret_fetch_failure_events(capsys, monkeypatch):
    import src.infra.secrets.manager as secrets_manager_runtime

    class _FailingProvider:
        async def get(self, name: str):
            raise RuntimeError(f"boom:{name}")

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
    import src.service.integrations.nango_client as nango_runtime

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
