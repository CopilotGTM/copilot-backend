"""OpenTelemetry initialization shared by FastAPI and all Celery workers.

Call ``init_otel(service_name_suffix)`` once at process startup.
Disabled when OTEL_ENABLED is false or OTEL_EXPORTER_OTLP_ENDPOINT is empty.

Instruments: Celery, httpx, requests, SQLAlchemy, Redis.
Also bridges Python ``logging`` so every log record gets trace_id/span_id.
"""

from __future__ import annotations

import logging


class _OtelLogSanitizer(logging.Filter):
    """Strip low-value source-location metadata from exported OTel logs."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - matches logging API
        record.pathname = ""
        record.filename = ""
        record.module = ""
        record.funcName = ""
        record.lineno = 0
        if hasattr(record, "color_message"):
            record.color_message = ""
        return True


_otel_initialized = False
_otel_log_handler = None


async def async_init_otel(service_name_suffix: str = "") -> None:
    """Bootstrap OTel tracing + log bridge if configured.

    Async version — loads OTLP auth headers from Key Vault before
    initializing exporters. Use in FastAPI lifespan.
    """
    global _otel_initialized
    if _otel_initialized:
        return

    from src.infra.config.configs import otel_config

    if not otel_config.OTEL_ENABLED or not otel_config.OTEL_EXPORTER_OTLP_ENDPOINT:
        return

    # Load auth header from Key Vault before creating exporters
    await otel_config.load_headers()

    _otel_initialized = True

    _setup_tracing(otel_config, service_name_suffix)
    _setup_logging(otel_config, service_name_suffix)
    _auto_instrument()


def init_otel(service_name_suffix: str = "") -> None:
    """Bootstrap OTel tracing + log bridge if configured.

    Sync version for Celery workers — uses run_async() to load
    OTLP auth headers from Key Vault.
    """
    global _otel_initialized
    if _otel_initialized:
        return

    from src.infra.config.configs import otel_config

    if not otel_config.OTEL_ENABLED or not otel_config.OTEL_EXPORTER_OTLP_ENDPOINT:
        return

    # Load auth header from Key Vault (sync bridge for workers)
    if not otel_config._credentials_from_env and otel_config._credentials_loaded_at is None:
        try:
            from src.infra.utils.async_runner import run_async

            run_async(otel_config.load_headers())
        except Exception:
            logging.getLogger(__name__).warning(
                "Could not load OTel headers from Key Vault in sync context"
            )

    _otel_initialized = True

    _setup_tracing(otel_config, service_name_suffix)
    _setup_logging(otel_config, service_name_suffix)
    _auto_instrument()


def _parse_headers(raw: str) -> dict[str, str]:
    """Parse ``key=value,key2=value2`` into a dict."""
    if not raw:
        return {}
    headers: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            headers[k.strip()] = v.strip()
    return headers


def _resource(otel_config, suffix: str):
    from opentelemetry.sdk.resources import Resource

    name = otel_config.OTEL_SERVICE_NAME
    if suffix:
        name = f"{name}{suffix}"
    return Resource.create(
        {
            "service.name": name,
            "deployment.environment": __import__("os").getenv("ENV", "dev"),
        }
    )


def _setup_tracing(otel_config, suffix: str) -> None:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

    provider = TracerProvider(
        resource=_resource(otel_config, suffix),
        sampler=TraceIdRatioBased(otel_config.OTEL_TRACES_SAMPLE_RATE),
    )

    exporter = OTLPSpanExporter(
        endpoint=f"{otel_config.OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces",
        headers=_parse_headers(otel_config.OTEL_EXPORTER_OTLP_HEADERS),
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


def _setup_logging(otel_config, suffix: str) -> None:
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    log_provider = LoggerProvider(resource=_resource(otel_config, suffix))

    log_exporter = OTLPLogExporter(
        endpoint=f"{otel_config.OTEL_EXPORTER_OTLP_ENDPOINT}/v1/logs",
        headers=_parse_headers(otel_config.OTEL_EXPORTER_OTLP_HEADERS),
    )
    log_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    set_logger_provider(log_provider)

    # Store handler so get_logger() can attach it to each app logger
    # (app loggers use propagate=False, so root handler wouldn't see them)
    global _otel_log_handler
    _otel_log_handler = LoggingHandler(level=logging.INFO, logger_provider=log_provider)
    _otel_log_handler.addFilter(_OtelLogSanitizer())
    # Also attach to root for any loggers that DO propagate
    logging.getLogger().addHandler(_otel_log_handler)


def get_otel_log_handler():
    """Return the OTel log handler if initialized, else None."""
    return _otel_log_handler


def instrument_fastapi_app(app) -> None:
    """Instrument the shared FastAPI app instance once it exists."""
    from src.infra.config.configs import otel_config

    if not otel_config.OTEL_ENABLED or not otel_config.OTEL_EXPORTER_OTLP_ENDPOINT:
        return

    if getattr(app.state, "_otel_fastapi_instrumented", False):
        return

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        return

    try:
        FastAPIInstrumentor.instrument_app(app)
        app.state._otel_fastapi_instrumented = True
    except Exception:
        logging.getLogger(__name__).exception("Failed to instrument FastAPI app with OpenTelemetry")


def _auto_instrument() -> None:
    """Best-effort auto-instrumentation — skip silently if a library isn't installed."""

    try:
        from opentelemetry.instrumentation.celery import CeleryInstrumentor

        CeleryInstrumentor().instrument()
    except ImportError:
        logging.getLogger(__name__).debug("Celery instrumentation not installed")
    except Exception:
        logging.getLogger(__name__).debug("Failed to auto-instrument Celery", exc_info=True)

    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        # Exclude Supabase auth calls — supabase-py uses http2=True internally
        # and OTel's httpx patching breaks HTTP/2 framing
        from src.infra.config.configs import supabase_config

        excluded = supabase_config.SUPABASE_URL.rstrip("/") if supabase_config.SUPABASE_URL else ""
        if excluded:
            import os

            existing = os.getenv("OTEL_PYTHON_HTTPX_EXCLUDED_URLS", "")
            combined = f"{existing},{excluded}" if existing else excluded
            os.environ["OTEL_PYTHON_HTTPX_EXCLUDED_URLS"] = combined

        HTTPXClientInstrumentor().instrument()
    except ImportError:
        logging.getLogger(__name__).debug("httpx instrumentation not installed")
    except Exception:
        logging.getLogger(__name__).debug("Failed to auto-instrument httpx", exc_info=True)

    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        RequestsInstrumentor().instrument()
    except ImportError:
        logging.getLogger(__name__).debug("requests instrumentation not installed")
    except Exception:
        logging.getLogger(__name__).debug("Failed to auto-instrument requests", exc_info=True)

    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument()
    except ImportError:
        logging.getLogger(__name__).debug("SQLAlchemy instrumentation not installed")
    except Exception:
        logging.getLogger(__name__).debug("Failed to auto-instrument SQLAlchemy", exc_info=True)

    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor

        RedisInstrumentor().instrument()
    except ImportError:
        logging.getLogger(__name__).debug("Redis instrumentation not installed")
    except Exception:
        logging.getLogger(__name__).debug("Failed to auto-instrument Redis", exc_info=True)
