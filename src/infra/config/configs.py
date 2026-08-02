"""Central runtime configuration objects and typed environment-backed config blocks.

Each config class owns a subsystem boundary (master DB, integrations, rate limits,
uploads, etc.) and should be read directly instead of replicating settings in
callers.
"""

import asyncio
import os
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

import google.auth
from dotenv import load_dotenv
from google.auth.credentials import Credentials

# Load env vars before constructing config classes so defaults can resolve correctly.
load_dotenv(override=False)

from ..utils.logger import get_logger
from .constant import CONFIG_MAP

logger = get_logger()
T = TypeVar("T")


def _run_coro_sync(coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine from sync startup code."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: T | None = None
    error: BaseException | None = None

    def _runner() -> None:
        nonlocal result, error
        try:
            result = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover
            error = exc

    thread = threading.Thread(target=_runner, name="security-secret-loader", daemon=True)
    thread.start()
    thread.join()

    if error is not None:
        raise error

    return result  # type: ignore[return-value]


class OTelConfig:
    """Configuration for OpenTelemetry tracing and log export.

    Active when OTEL_ENABLED resolves true and OTEL_EXPORTER_OTLP_ENDPOINT is set.
    Local defaults to disabled unless explicitly opted in.
    Grafana Cloud OTLP endpoint example:
        https://otlp-gateway-prod-us-west-0.grafana.net/otlp

    Auth header (OTEL_EXPORTER_OTLP_HEADERS) is loaded from env var (local dev)
    or Azure Key Vault (production) with a configurable TTL cache.
    """

    # Key Vault secret name for the auth header
    SECRET_NAME_OTLP_HEADERS: str = "otel-exporter-otlp-headers"

    OTEL_SERVICE_NAME: str = os.getenv("OTEL_SERVICE_NAME", "copilotgtm-backend")
    _OTEL_ENABLED_RAW = os.getenv("OTEL_ENABLED")
    OTEL_ENABLED: bool = (
        _OTEL_ENABLED_RAW.lower() == "true"
        if _OTEL_ENABLED_RAW is not None
        else os.getenv("ENV", "dev") != "local"
    )
    OTEL_EXPORTER_OTLP_ENDPOINT: str = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    OTEL_EXPORTER_OTLP_HEADERS: str = os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")
    OTEL_TRACES_SAMPLE_RATE: float = float(os.getenv("OTEL_TRACES_SAMPLE_RATE", "0.2"))
    # Keep metric export cadence configurable so lower environments can trade freshness for cost.
    OTEL_METRICS_EXPORT_INTERVAL_MS: int = int(
        os.getenv("OTEL_METRICS_EXPORT_INTERVAL_MS", "60000")
    )

    _credentials_from_env: bool = bool(os.getenv("OTEL_EXPORTER_OTLP_HEADERS"))
    _credentials_ttl_hours: float = float(os.getenv("OTEL_CREDENTIALS_TTL_HOURS", "6"))
    _credentials_loaded_at: float | None = None
    _refresh_lock: asyncio.Lock | None = None

    async def load_headers(self) -> None:
        """Load OTLP auth headers from env or Key Vault with TTL cache.

        Env var takes precedence — when OTEL_EXPORTER_OTLP_HEADERS is set,
        Key Vault is skipped entirely (for local development).
        """
        if self._credentials_from_env:
            return

        if not self._is_expired():
            return

        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        async with self._refresh_lock:
            if not self._is_expired():
                return
            await self._load_from_vault()

    def _is_expired(self) -> bool:
        if self._credentials_loaded_at is None:
            return True
        import time

        elapsed_hours = (time.time() - self._credentials_loaded_at) / 3600
        return elapsed_hours >= self._credentials_ttl_hours

    async def _load_from_vault(self) -> None:
        import time

        from src.infra.secrets.manager import master_secrets_manager

        logger.info("Loading OTel OTLP headers from Key Vault (TTL refresh)")
        value = await master_secrets_manager.get(self.SECRET_NAME_OTLP_HEADERS)
        if value:
            self.OTEL_EXPORTER_OTLP_HEADERS = value
            logger.info("OTel OTLP headers cached (TTL=%.1fh)", self._credentials_ttl_hours)
        else:
            logger.error("OTel OTLP headers missing from Key Vault")
        self._credentials_loaded_at = time.time()


class SentryConfig:
    """Configuration for Sentry error tracking.

    DSN is safe to expose (it only identifies where to send events, not a secret).
    Disable in local dev by leaving SENTRY_DSN empty.
    """

    SENTRY_DSN: str = os.getenv("SENTRY_DSN", "")
    SENTRY_ENVIRONMENT: str = os.getenv("ENV", "dev")
    # Default to no automatic PII capture; explicit allow-in is safer for a multi-tenant backend.
    SENTRY_SEND_DEFAULT_PII: bool = os.getenv("SENTRY_SEND_DEFAULT_PII", "false").lower() == "true"
    SENTRY_TRACES_SAMPLE_RATE: float = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0"))


class SupabaseConfig:
    """Configuration for Supabase authentication.

    Credentials are loaded from env vars (local dev) or Azure Key Vault
    (production) with a configurable TTL cache. When all four env vars are
    set, Key Vault is skipped entirely.
    """

    # Key Vault secret names
    SECRET_NAME_URL: str = "supabase-url"
    SECRET_NAME_ANON_KEY: str = "supabase-anon-key"
    SECRET_NAME_SERVICE_ROLE_KEY: str = "supabase-service-role-key"
    SECRET_NAME_JWT_SECRET: str = "supabase-jwt-secret"

    # Cached values (populated from env or Key Vault)
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_ANON_KEY: str = os.getenv("SUPABASE_ANON_KEY", "")
    SUPABASE_SERVICE_ROLE_KEY: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    SUPABASE_JWT_SECRET: str = os.getenv("SUPABASE_JWT_SECRET", "")

    # Enable/disable Supabase auth
    ENABLE_SUPABASE_AUTH: bool = os.getenv("ENABLE_SUPABASE_AUTH", "true").lower() == "true"

    _credentials_from_env: bool = bool(
        os.getenv("SUPABASE_URL")
        and os.getenv("SUPABASE_ANON_KEY")
        and os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        and os.getenv("SUPABASE_JWT_SECRET")
    )
    _credentials_ttl_hours: float = float(os.getenv("SUPABASE_CREDENTIALS_TTL_HOURS", "6"))
    _credentials_loaded_at: float | None = None
    _refresh_lock: asyncio.Lock | None = None

    async def get_credentials(
        self,
    ) -> tuple[str, str, str, str]:
        """Return (url, anon_key, service_role_key, jwt_secret).

        Env vars take precedence — when all four are set, Key Vault is
        skipped entirely (for local development).
        """
        if self._credentials_from_env:
            return (
                self.SUPABASE_URL,
                self.SUPABASE_ANON_KEY,
                self.SUPABASE_SERVICE_ROLE_KEY,
                self.SUPABASE_JWT_SECRET,
            )

        if not self._is_expired():
            return (
                self.SUPABASE_URL,
                self.SUPABASE_ANON_KEY,
                self.SUPABASE_SERVICE_ROLE_KEY,
                self.SUPABASE_JWT_SECRET,
            )

        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        async with self._refresh_lock:
            if not self._is_expired():
                return (
                    self.SUPABASE_URL,
                    self.SUPABASE_ANON_KEY,
                    self.SUPABASE_SERVICE_ROLE_KEY,
                    self.SUPABASE_JWT_SECRET,
                )
            await self._load_from_vault()

        return (
            self.SUPABASE_URL,
            self.SUPABASE_ANON_KEY,
            self.SUPABASE_SERVICE_ROLE_KEY,
            self.SUPABASE_JWT_SECRET,
        )

    def _is_expired(self) -> bool:
        if self._credentials_loaded_at is None:
            return True
        import time

        elapsed_hours = (time.time() - self._credentials_loaded_at) / 3600
        return elapsed_hours >= self._credentials_ttl_hours

    async def _load_from_vault(self) -> None:
        import time

        from src.infra.secrets.manager import master_secrets_manager

        logger.info("Loading Supabase credentials from Key Vault (TTL refresh)")
        self.SUPABASE_URL = await master_secrets_manager.get(self.SECRET_NAME_URL) or ""
        self.SUPABASE_ANON_KEY = await master_secrets_manager.get(self.SECRET_NAME_ANON_KEY) or ""
        self.SUPABASE_SERVICE_ROLE_KEY = (
            await master_secrets_manager.get(self.SECRET_NAME_SERVICE_ROLE_KEY) or ""
        )
        self.SUPABASE_JWT_SECRET = (
            await master_secrets_manager.get(self.SECRET_NAME_JWT_SECRET) or ""
        )
        self._credentials_loaded_at = time.time()

        if self.SUPABASE_URL and self.SUPABASE_JWT_SECRET:
            logger.info("Supabase credentials cached (TTL=%.1fh)", self._credentials_ttl_hours)
        else:
            logger.error("Supabase credentials missing from Key Vault")


class DatabaseConfig:
    """Database configuration.

    App-side QueuePool caches TCP connections to pgbouncer.
    pgbouncer handles backend PostgreSQL connection pooling.
    """

    DATABASE_HOST: str = os.getenv("DATABASE_HOST", "localhost")
    DATABASE_PORT: str = os.getenv("DATABASE_PORT", "5432")
    DATABASE_USER: str = os.getenv("DATABASE_USER", "postgres")
    DATABASE_PASSWORD: str = os.getenv("DATABASE_PASSWORD", "")
    DATABASE_NAME: str = os.getenv("DATABASE_NAME", "postgres")
    DATABASE_SSL_MODE: str = os.getenv("DATABASE_SSL_MODE", "require")
    DATABASE_SSL_ROOT_CERT: str | None = os.getenv("DATABASE_SSL_ROOT_CERT")
    USE_SHARED_TENANT_DATABASE: bool = (
        os.getenv("USE_SHARED_TENANT_DATABASE", "false").lower() == "true"
    )
    TENANT_DATABASE_USER: str = os.getenv("TENANT_DATABASE_USER", "")
    TENANT_DATABASE_PASSWORD: str = os.getenv("TENANT_DATABASE_PASSWORD", "")
    TENANT_DATABASE_PASSWORD_SECRET_NAME: str = os.getenv(
        "TENANT_DATABASE_PASSWORD_SECRET_NAME",
        "shared-tenant-db-password",
    )

    # App-side pool settings (caches TCP connections to pgbouncer)
    DATABASE_POOL_SIZE: int = int(os.getenv("DATABASE_POOL_SIZE", "10"))
    DATABASE_MAX_OVERFLOW: int = int(os.getenv("DATABASE_MAX_OVERFLOW", "20"))
    DATABASE_POOL_TIMEOUT_SEC: int = int(os.getenv("DATABASE_POOL_TIMEOUT_SEC", "60"))
    POOL_RECYCLE_SEC: int = int(os.getenv("POOL_RECYCLE_SEC", "1800"))

    # Guard against hangs when DB/pgbouncer unavailable
    DATABASE_CONNECT_TIMEOUT_SEC: int = int(os.getenv("DATABASE_CONNECT_TIMEOUT_SEC", "10"))

    # Default server-side statement timeout (ms). 0 disables.
    DATABASE_STATEMENT_TIMEOUT_MS: int = int(os.getenv("DATABASE_STATEMENT_TIMEOUT_MS", "60000"))


class AppConfig:
    """Application configuration"""

    # API settings
    API_V1_STR: str = "/api/v1"
    PROJECT_NAME: str = "CopilotGTM Backend"

    # CORS settings
    BACKEND_CORS_ORIGINS: list = [
        "http://localhost:3000",
        "http://localhost:8080",
        "http://localhost:8000",
    ]

    # App URL
    APP_URL: str = os.getenv("APP_URL", "")

    # Security settings
    SECRET_KEY: str = os.getenv("SECRET_KEY", "").strip()

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    @property
    def voice_realtime_api_enabled(self) -> bool:
        """Feature flag for exposing the voice realtime HTTP routes."""
        return os.getenv("VOICE_REALTIME_API_ENABLED", "false").lower() == "true"


class SecuritySecretsConfig:
    """Runtime signing secrets sourced from env vars or Azure Key Vault."""

    SECRET_NAME_SESSION_SECRET: str = "session-secret"
    SECRET_NAME_OAUTH_STATE_SECRET: str = "oauth-state-secret-key"
    SECRET_NAME_VOICE_REALTIME_CONFIG_SECRET: str = "voice-realtime-config-secret"

    _session_secret: str = os.getenv("SESSION_SECRET", "").strip()
    _oauth_state_secret: str = os.getenv("SECRET_KEY", "").strip()
    _voice_realtime_config_secret: str = os.getenv("VOICE_REALTIME_CONFIG_SECRET", "").strip()

    _session_secret_from_env: bool = bool(_session_secret)
    _oauth_state_secret_from_env: bool = bool(_oauth_state_secret)
    _voice_realtime_config_secret_from_env: bool = bool(_voice_realtime_config_secret)

    _credentials_ttl_hours: float = float(os.getenv("SECURITY_SECRET_TTL_HOURS", "6"))
    _credentials_loaded_at: float | None = None
    _refresh_lock: asyncio.Lock | None = None

    async def preload_required(self, *, include_voice_realtime: bool = False) -> None:
        """Eagerly load secrets that startup and request handlers need.\n\n        Voice-realtime secrets are optional, so this flag avoids unnecessary\n        Key Vault calls when voice-realtime is not in use.\n"""
        await self.get_session_secret()
        await self.get_oauth_state_secret()
        if include_voice_realtime:
            await self.get_voice_realtime_config_secret()

    async def get_session_secret(self) -> str:
        """Return session secret used by auth/session cookies and JWT validation."""
        await self._ensure_loaded(include_voice_realtime=False)
        if self._session_secret:
            return self._session_secret
        raise RuntimeError(
            "SESSION_SECRET unavailable from env or Key Vault secret "
            f"'{self.SECRET_NAME_SESSION_SECRET}'"
        )

    def get_session_secret_sync(self) -> str:
        """Synchronous session secret accessor for startup paths that cannot await."""
        return _run_coro_sync(self.get_session_secret())

    async def get_oauth_state_secret(self) -> str:
        """Return OAuth callback state secret used in authorization flows."""
        await self._ensure_loaded(include_voice_realtime=False)
        if self._oauth_state_secret:
            return self._oauth_state_secret
        raise RuntimeError(
            "SECRET_KEY unavailable from env or Key Vault secret "
            f"'{self.SECRET_NAME_OAUTH_STATE_SECRET}'"
        )

    async def get_voice_realtime_config_secret(self) -> str:
        """Return voice-realtime secret when voice endpoints authenticate calls."""
        await self._ensure_loaded(include_voice_realtime=True)
        if self._voice_realtime_config_secret:
            return self._voice_realtime_config_secret
        raise RuntimeError(
            "VOICE_REALTIME_CONFIG_SECRET unavailable from env or Key Vault secret "
            f"'{self.SECRET_NAME_VOICE_REALTIME_CONFIG_SECRET}'"
        )

    async def _ensure_loaded(self, *, include_voice_realtime: bool) -> None:
        if not self._needs_refresh(include_voice_realtime=include_voice_realtime):
            return

        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        async with self._refresh_lock:
            if not self._needs_refresh(include_voice_realtime=include_voice_realtime):
                return
            await self._load_from_vault(include_voice_realtime=include_voice_realtime)

    def _needs_refresh(self, *, include_voice_realtime: bool) -> bool:
        if not self._session_secret_from_env and not self._session_secret:
            return True
        if not self._oauth_state_secret_from_env and not self._oauth_state_secret:
            return True
        if (
            include_voice_realtime
            and not self._voice_realtime_config_secret_from_env
            and not self._voice_realtime_config_secret
        ):
            return True
        if self._credentials_loaded_at is None:
            return True

        import time

        elapsed_hours = (time.time() - self._credentials_loaded_at) / 3600
        return elapsed_hours >= self._credentials_ttl_hours

    async def _load_from_vault(self, *, include_voice_realtime: bool) -> None:
        import time

        from src.infra.secrets.manager import master_secrets_manager

        logger.info("Loading security signing secrets from Key Vault (TTL refresh)")

        if not self._session_secret_from_env:
            self._session_secret = (
                await master_secrets_manager.get(self.SECRET_NAME_SESSION_SECRET) or ""
            ).strip()
        if not self._oauth_state_secret_from_env:
            self._oauth_state_secret = (
                await master_secrets_manager.get(self.SECRET_NAME_OAUTH_STATE_SECRET) or ""
            ).strip()
        if include_voice_realtime and not self._voice_realtime_config_secret_from_env:
            self._voice_realtime_config_secret = (
                await master_secrets_manager.get(self.SECRET_NAME_VOICE_REALTIME_CONFIG_SECRET)
                or ""
            ).strip()

        self._credentials_loaded_at = time.time()
        logger.info(
            "Security signing secret cache refreshed (TTL=%.1fh)",
            self._credentials_ttl_hours,
            extra={
                "event": "security_signing_secrets_cached",
                "has_session_secret": bool(self._session_secret),
                "has_oauth_state_secret": bool(self._oauth_state_secret),
                "has_voice_realtime_config_secret": bool(self._voice_realtime_config_secret),
                "voice_realtime_requested": include_voice_realtime,
            },
        )


class OpenAIConfig:
    """Configuration for OpenAI services"""

    AZURE_OPENAI_API_KEY: str | None = os.getenv("AZURE_OPENAI_API_KEY")
    AZURE_OPENAI_ENDPOINT: str | None = os.getenv("AZURE_OPENAI_ENDPOINT")


class EmbedderConfig:
    """Configuration for Embedder service"""

    AZURE_EMBEDDER_OPENAI_API_KEY: str | None = os.getenv("AZURE_EMBEDDER_OPENAI_API_KEY")
    AZURE_EMBEDDER_OPENAI_ENDPOINT: str | None = os.getenv("AZURE_EMBEDDER_OPENAI_ENDPOINT")
    AZURE_EMBEDDER_DEPLOYMENT: str | None = os.getenv("AZURE_EMBEDDER_DEPLOYMENT")


class TranscriptionConfig:
    """Configuration for audio transcription providers.

    AssemblyAI is the default ASR provider for new audio work. Azure OpenAI
    remains configurable as a fallback provider during the migration window.
    """

    ASR_PROVIDER: str = os.getenv("ASR_PROVIDER", "assemblyai").strip().lower()

    # AssemblyAI pre-recorded transcription. The API key can come from env/K8s secret
    # or the master Key Vault secret below when async code calls get_assemblyai_api_key().
    SECRET_NAME_ASSEMBLYAI_API_KEY: str = "assemblyai-api-key"
    ASSEMBLYAI_API_KEY: str | None = os.getenv("ASSEMBLYAI_API_KEY")
    ASSEMBLYAI_BASE_URL: str = os.getenv("ASSEMBLYAI_BASE_URL", "https://api.assemblyai.com")
    ASSEMBLYAI_SPEECH_MODEL: str = os.getenv("ASSEMBLYAI_SPEECH_MODEL", "universal-3-5-pro")
    ASSEMBLYAI_LANGUAGE_DETECTION: bool = (
        os.getenv("ASSEMBLYAI_LANGUAGE_DETECTION", "true").lower() == "true"
    )
    ASSEMBLYAI_POLL_INTERVAL_SECONDS: float = float(
        os.getenv("ASSEMBLYAI_POLL_INTERVAL_SECONDS", "2")
    )
    ASSEMBLYAI_MAX_POLL_ATTEMPTS: int = int(os.getenv("ASSEMBLYAI_MAX_POLL_ATTEMPTS", "45"))
    _assemblyai_api_key_from_env: bool = bool(os.getenv("ASSEMBLYAI_API_KEY"))
    _assemblyai_credentials_ttl_hours: float = float(
        os.getenv("ASSEMBLYAI_CREDENTIALS_TTL_HOURS", "6")
    )
    _assemblyai_credentials_loaded_at: float | None = None
    _assemblyai_refresh_lock: asyncio.Lock | None = None

    AZURE_TRANSCRIPTION_API_KEY: str | None = os.getenv(
        "AZURE_TRANSCRIPTION_API_KEY", os.getenv("AZURE_OPENAI_API_KEY")
    )
    AZURE_TRANSCRIPTION_ENDPOINT: str | None = os.getenv(
        "AZURE_TRANSCRIPTION_ENDPOINT", os.getenv("AZURE_OPENAI_ENDPOINT")
    )
    AZURE_TRANSCRIPTION_DEPLOYMENT: str = os.getenv(
        "AZURE_TRANSCRIPTION_DEPLOYMENT", "gpt-4o-transcribe"
    )
    AZURE_TRANSCRIPTION_API_VERSION: str = os.getenv(
        "AZURE_TRANSCRIPTION_API_VERSION", "2025-03-01-preview"
    )

    async def get_assemblyai_api_key(self) -> str | None:
        """Return the AssemblyAI API key from env/K8s secret or Key Vault."""
        if self._assemblyai_api_key_from_env:
            return self.ASSEMBLYAI_API_KEY

        if not self._assemblyai_key_expired():
            return self.ASSEMBLYAI_API_KEY

        if self._assemblyai_refresh_lock is None:
            self._assemblyai_refresh_lock = asyncio.Lock()

        async with self._assemblyai_refresh_lock:
            if not self._assemblyai_key_expired():
                return self.ASSEMBLYAI_API_KEY
            await self._load_assemblyai_key_from_vault()

        return self.ASSEMBLYAI_API_KEY

    def _assemblyai_key_expired(self) -> bool:
        if not self.ASSEMBLYAI_API_KEY:
            return True
        if self._assemblyai_credentials_loaded_at is None:
            return True

        import time

        elapsed_hours = (time.time() - self._assemblyai_credentials_loaded_at) / 3600
        return elapsed_hours >= self._assemblyai_credentials_ttl_hours

    async def _load_assemblyai_key_from_vault(self) -> None:
        import time

        from src.infra.secrets.manager import master_secrets_manager

        logger.info("Loading AssemblyAI API key from Key Vault (TTL refresh)")
        self.ASSEMBLYAI_API_KEY = (
            await master_secrets_manager.get(self.SECRET_NAME_ASSEMBLYAI_API_KEY) or ""
        ).strip()
        self._assemblyai_credentials_loaded_at = time.time()

        if self.ASSEMBLYAI_API_KEY:
            logger.info(
                "AssemblyAI API key cached (TTL=%.1fh)",
                self._assemblyai_credentials_ttl_hours,
            )
        else:
            logger.error("AssemblyAI API key missing from Key Vault")


class CloudStorageConfig:
    """Configuration for Cloud Storage"""

    CLOUD_PROVIDER: str = os.getenv("CLOUD_PROVIDER", "aws")
    APPLICATION_STORAGE_NAME: str | None = os.getenv("APPLICATION_STORAGE_NAME")
    ORGANISATION_STORAGE_NAME: str | None = os.getenv("ORGANISATION_STORAGE_NAME")
    APPLICATION_REGION: str | None = os.getenv("APPLICATION_REGION")


class ResendConfig:
    """Configuration for Resend email service.

    API key is stored in Azure Key Vault (master vault) and cached
    with a configurable TTL (default 6 hours). Can be overridden via env var
    (RESEND_API_KEY) for local development.
    """

    # Key Vault secret name
    SECRET_NAME_API_KEY: str = "resend-api-key"

    # Cached API key (populated by get_api_key)
    _api_key: str | None = os.getenv("RESEND_API_KEY")
    _credentials_ttl_hours: float = float(os.getenv("RESEND_CREDENTIALS_TTL_HOURS", "6"))
    _credentials_loaded_at: float | None = None
    _credentials_from_env: bool = bool(os.getenv("RESEND_API_KEY"))
    _refresh_lock: asyncio.Lock | None = None

    FROM_EMAIL: str | None = os.getenv("FROM_EMAIL")

    async def get_api_key(self) -> str | None:
        """Return cached API key, refreshing from Key Vault if TTL expired."""
        if self._credentials_from_env:
            return self._api_key

        if not self._is_expired():
            return self._api_key

        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        async with self._refresh_lock:
            if not self._is_expired():
                return self._api_key
            await self._load_from_vault()

        return self._api_key

    def _is_expired(self) -> bool:
        if self._credentials_loaded_at is None:
            return True
        import time

        elapsed_hours = (time.time() - self._credentials_loaded_at) / 3600
        return elapsed_hours >= self._credentials_ttl_hours

    async def _load_from_vault(self) -> None:
        import time

        from src.infra.secrets.manager import master_secrets_manager

        logger.info("Loading Resend API key from Key Vault (TTL refresh)")
        self._api_key = await master_secrets_manager.get(self.SECRET_NAME_API_KEY)
        self._credentials_loaded_at = time.time()

        if self._api_key:
            logger.info("Resend API key cached (TTL=%.1fh)", self._credentials_ttl_hours)
        else:
            logger.error("Resend API key missing from Key Vault")


class SuperAdminConfig:
    """Configuration for Super Admin"""

    SUPER_ADMIN_EMAIL: str | None = os.getenv("SUPER_ADMIN_EMAIL")


class ApolloConfig:
    """Configuration for Apollo.io people enrichment.

    API key is stored in Azure Key Vault (master vault) and cached
    with a configurable TTL (default 6 hours). The first call to
    ``get_api_key()`` fetches from Key Vault; subsequent calls return
    from memory until the TTL expires.

    The key can also be set via env var (APOLLO_API_KEY) for local
    development — when present, Key Vault is skipped entirely.

    API Docs: https://docs.apollo.io/reference/introduction
    """

    APOLLO_BASE_URL: str = os.getenv("APOLLO_BASE_URL", "https://api.apollo.io/api/v1")

    # Key Vault secret name
    SECRET_NAME_API_KEY: str = "apollo-api-key"

    # Cached API key (populated by get_api_key)
    _api_key: str | None = os.getenv("APOLLO_API_KEY")

    # TTL for cached key (hours)
    _credentials_ttl_hours: float = float(os.getenv("APOLLO_CREDENTIALS_TTL_HOURS", "6"))
    _credentials_loaded_at: float | None = None
    _credentials_from_env: bool = bool(os.getenv("APOLLO_API_KEY"))
    _refresh_lock: asyncio.Lock | None = None

    async def get_api_key(self) -> str | None:
        """Return cached API key, refreshing from Key Vault if TTL expired."""
        if self._credentials_from_env:
            return self._api_key

        if not self._is_expired():
            return self._api_key

        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        async with self._refresh_lock:
            if not self._is_expired():
                return self._api_key
            await self._load_from_vault()

        return self._api_key

    def _is_expired(self) -> bool:
        if self._credentials_loaded_at is None:
            return True
        import time

        elapsed_hours = (time.time() - self._credentials_loaded_at) / 3600
        return elapsed_hours >= self._credentials_ttl_hours

    async def _load_from_vault(self) -> None:
        import time

        from src.infra.secrets.manager import master_secrets_manager

        logger.info("Loading Apollo API key from Key Vault (TTL refresh)")
        self._api_key = await master_secrets_manager.get(self.SECRET_NAME_API_KEY)
        self._credentials_loaded_at = time.time()

        if self._api_key:
            logger.info("Apollo API key cached (TTL=%.1fh)", self._credentials_ttl_hours)
        else:
            logger.error("Apollo API key missing from Key Vault")


class ExaConfig:
    """Configuration for application-level Exa web search.

    Local development reads ``EXA_API_KEY`` directly. Deployed agents load the
    shared key from the master Key Vault and cache it so every search does not
    add a secrets-provider round trip.
    """

    SECRET_NAME_API_KEY: str = "exa-api-key"

    _api_key: str | None = (os.getenv("EXA_API_KEY") or "").strip() or None
    _credentials_ttl_hours: float = float(os.getenv("EXA_CREDENTIALS_TTL_HOURS", "6"))
    _credentials_loaded_at: float | None = None
    _credentials_from_env: bool = bool(_api_key)
    _refresh_lock: asyncio.Lock | None = None

    async def get_api_key(self) -> str | None:
        """Return the environment key or a TTL-cached master-vault key."""
        if self._credentials_from_env:
            return self._api_key
        if not self._is_expired():
            return self._api_key

        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        async with self._refresh_lock:
            if not self._is_expired():
                return self._api_key
            await self._load_from_vault()
        return self._api_key

    async def refresh_rejected_api_key(self, rejected_key: str) -> str | None:
        """Refresh a rejected vault key once without stampeding Key Vault.

        Environment keys remain operator-controlled and are never replaced from
        Key Vault. Concurrent workers that rejected the same cached key share
        the first refresh because later callers see that the value has changed.
        """
        if self._credentials_from_env:
            return self._api_key

        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        async with self._refresh_lock:
            if self._api_key and self._api_key != rejected_key:
                return self._api_key
            await self._load_from_vault()
        return self._api_key

    def _is_expired(self) -> bool:
        if self._credentials_loaded_at is None:
            return True
        import time

        elapsed_hours = (time.time() - self._credentials_loaded_at) / 3600
        return elapsed_hours >= self._credentials_ttl_hours

    async def _load_from_vault(self) -> None:
        import time

        from src.infra.secrets.manager import master_secrets_manager

        logger.info("Loading Exa API key from Key Vault (TTL refresh)")
        self._api_key = (
            await master_secrets_manager.get(self.SECRET_NAME_API_KEY) or ""
        ).strip() or None
        self._credentials_loaded_at = time.time()

        if self._api_key:
            logger.info("Exa API key cached (TTL=%.1fh)", self._credentials_ttl_hours)
        else:
            logger.error("Exa API key missing from Key Vault")


class RecallConfig:
    """Configuration for Recall.ai meeting bot access and webhook verification.

    Shared by the API webhook route and the deal worker tasks so both sides of
    the flow read the same provider region, timeout, and secret-loading rules.
    """

    RECALL_REGION: str = os.getenv("RECALL_REGION", "us-west-2")
    RECALL_TIMEOUT_SECONDS: float = float(os.getenv("RECALL_TIMEOUT_SECONDS", "30"))
    RECALL_BOT_NAME: str = os.getenv("RECALL_BOT_NAME", "Notetaker")

    SECRET_NAME_API_KEY: str = "recall-api-key"
    SECRET_NAME_WEBHOOK_SECRET: str = "recall-webhook-secret"

    _api_key: str | None = os.getenv("RECALL_API_KEY")
    _webhook_secret: str | None = os.getenv("RECALL_WEBHOOK_SECRET")
    _credentials_ttl_hours: float = float(os.getenv("RECALL_CREDENTIALS_TTL_HOURS", "6"))
    _credentials_loaded_at: float | None = None
    _api_key_from_env: bool = bool(os.getenv("RECALL_API_KEY"))
    _webhook_secret_from_env: bool = bool(os.getenv("RECALL_WEBHOOK_SECRET"))
    # One lock/TTL covers both secrets because they usually rotate together and
    # the flow commonly needs both during the same deployment window.
    _refresh_lock: asyncio.Lock | None = None

    async def get_api_key(self) -> str | None:
        """Return cached API key, refreshing from Key Vault if TTL expired."""
        if self._api_key_from_env:
            return self._api_key

        if not self._is_expired():
            return self._api_key

        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        async with self._refresh_lock:
            if not self._is_expired():
                return self._api_key
            await self._load_from_vault()

        return self._api_key

    async def get_webhook_secret(self) -> str | None:
        """Return cached webhook secret, refreshing from Key Vault if TTL expired."""
        if self._webhook_secret_from_env:
            return self._webhook_secret

        if not self._is_expired():
            return self._webhook_secret

        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        async with self._refresh_lock:
            if not self._is_expired():
                return self._webhook_secret
            await self._load_from_vault()

        return self._webhook_secret

    def _is_expired(self) -> bool:
        if self._credentials_loaded_at is None:
            return True
        import time

        elapsed_hours = (time.time() - self._credentials_loaded_at) / 3600
        return elapsed_hours >= self._credentials_ttl_hours

    async def _load_from_vault(self) -> None:
        import time

        from src.infra.secrets.manager import master_secrets_manager

        logger.info("Loading Recall credentials from Key Vault (TTL refresh)")
        # Allow local/test environments to override only one credential without
        # forcing the other one out of Key Vault.
        if not self._api_key_from_env:
            self._api_key = await master_secrets_manager.get(self.SECRET_NAME_API_KEY)
        if not self._webhook_secret_from_env:
            self._webhook_secret = await master_secrets_manager.get(self.SECRET_NAME_WEBHOOK_SECRET)
        self._credentials_loaded_at = time.time()

        if self._api_key and self._webhook_secret:
            logger.info("Recall credentials cached (TTL=%.1fh)", self._credentials_ttl_hours)
        else:
            logger.error("Recall credentials missing from Key Vault")


class TeamsConfig:
    """Configuration for Microsoft Teams Bot integration.

    Bot credentials are stored in Azure Key Vault (master vault) and cached
    with a configurable TTL (default 6 hours). The first call to
    ``get_bot_credentials()`` fetches from Key Vault; subsequent calls return
    from memory until the TTL expires.

    Credentials can also be set via env vars (TEAMS_BOT_APP_ID, etc.) for
    local development — when present, Key Vault is skipped entirely.

    Environment variables:
    - TEAMS_ENABLED: Enable/disable Teams bot functionality
    - TEAMS_AUTH_LOGIN_URL: URL to your login page for Teams auth flow
    - TEAMS_AUTH_CALLBACK_URL: Callback URL after login
    - TEAMS_BOT_CREDENTIALS_TTL_HOURS: TTL for cached credentials (default 6)
    """

    # Feature flag - can be overridden via env var
    TEAMS_ENABLED: bool = os.getenv("TEAMS_ENABLED", "true").lower() == "true"

    # Key Vault secret names (these are the names stored in master Key Vault)
    SECRET_NAME_BOT_APP_ID: str = "teams-bot-app-id"
    SECRET_NAME_BOT_APP_SECRET: str = "teams-bot-app-secret"
    SECRET_NAME_BOT_TENANT_ID: str = "teams-bot-tenant-id"

    # Cached credential values (populated by get_bot_credentials)
    BOT_APP_ID: str | None = os.getenv("TEAMS_BOT_APP_ID")
    BOT_APP_SECRET: str | None = os.getenv("TEAMS_BOT_APP_SECRET")
    BOT_TENANT_ID: str | None = os.getenv("TEAMS_BOT_TENANT_ID")

    # TTL for cached credentials (hours)
    _credentials_ttl_hours: float = float(os.getenv("TEAMS_BOT_CREDENTIALS_TTL_HOURS", "6"))
    _credentials_loaded_at: float | None = None  # epoch timestamp
    _credentials_from_env: bool = bool(os.getenv("TEAMS_BOT_APP_ID"))
    _refresh_lock: asyncio.Lock | None = None  # lazy-init (no event loop at import)

    async def get_bot_credentials(self) -> tuple[str | None, str | None, str | None]:
        """Return cached bot credentials, refreshing from Key Vault if TTL expired.

        Thread-safe via double-checked locking. If credentials are set via
        env vars, Key Vault is never called.

        Returns:
            Tuple of (app_id, app_secret, tenant_id).
        """
        # Env var override — never expires
        if self._credentials_from_env:
            return self.BOT_APP_ID, self.BOT_APP_SECRET, self.BOT_TENANT_ID

        if not self._is_expired():
            return self.BOT_APP_ID, self.BOT_APP_SECRET, self.BOT_TENANT_ID

        # Lazy-init lock (can't create at import time — no event loop yet)
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        async with self._refresh_lock:
            # Double-check after acquiring lock
            if not self._is_expired():
                return self.BOT_APP_ID, self.BOT_APP_SECRET, self.BOT_TENANT_ID

            await self._load_credentials_from_vault()

        return self.BOT_APP_ID, self.BOT_APP_SECRET, self.BOT_TENANT_ID

    def _is_expired(self) -> bool:
        """Check if cached credentials have exceeded the TTL."""
        if self._credentials_loaded_at is None:
            return True
        import time

        elapsed_hours = (time.time() - self._credentials_loaded_at) / 3600
        return elapsed_hours >= self._credentials_ttl_hours

    async def _load_credentials_from_vault(self) -> None:
        """Fetch bot credentials from Master Key Vault and cache them."""
        import time

        from src.infra.secrets.manager import master_secrets_manager

        logger.info("Loading Teams bot credentials from Key Vault (TTL refresh)")
        self.BOT_APP_ID = await master_secrets_manager.get(self.SECRET_NAME_BOT_APP_ID)
        self.BOT_APP_SECRET = await master_secrets_manager.get(self.SECRET_NAME_BOT_APP_SECRET)
        self.BOT_TENANT_ID = await master_secrets_manager.get(self.SECRET_NAME_BOT_TENANT_ID)
        self._credentials_loaded_at = time.time()

        if self.BOT_APP_ID and self.BOT_APP_SECRET:
            logger.info("Teams bot credentials cached (TTL=%.1fh)", self._credentials_ttl_hours)
        else:
            logger.error("Teams bot credentials missing from Key Vault")

    # Bot Framework auth validation
    TEAMS_BOT_OPENID_CONFIG_URL: str = os.getenv(
        "TEAMS_BOT_OPENID_CONFIG_URL",
        "https://login.botframework.com/v1/.well-known/openidconfiguration",
    )
    TEAMS_BOT_ALLOWED_ISSUERS: list[str] = [
        issuer.strip()
        for issuer in os.getenv("TEAMS_BOT_ALLOWED_ISSUERS", "https://api.botframework.com").split(
            ","
        )
        if issuer.strip()
    ]
    TEAMS_BOT_AUTH_JWKS_CACHE_TTL_SECONDS: int = int(
        os.getenv("TEAMS_BOT_AUTH_JWKS_CACHE_TTL_SECONDS", "86400")
    )
    TEAMS_BOT_AUTH_CLOCK_SKEW_SECONDS: int = int(
        os.getenv("TEAMS_BOT_AUTH_CLOCK_SKEW_SECONDS", "300")
    )
    TEAMS_BOT_AUTH_HTTP_TIMEOUT_SEC: float = float(
        os.getenv("TEAMS_BOT_AUTH_HTTP_TIMEOUT_SEC", "5")
    )
    TEAMS_WEBHOOK_DEDUPE_TTL_SECONDS: int = int(
        os.getenv("TEAMS_WEBHOOK_DEDUPE_TTL_SECONDS", "300")
    )

    # Auth URLs for the login flow
    # Derives from APP_URL if TEAMS_AUTH_LOGIN_URL is not explicitly set
    TEAMS_AUTH_LOGIN_URL: str = os.getenv(
        "TEAMS_AUTH_LOGIN_URL",
        f"{os.getenv('APP_URL', '').rstrip('/')}/bots/auth" if os.getenv("APP_URL") else "",
    )
    TEAMS_AUTH_CALLBACK_URL: str = os.getenv("TEAMS_AUTH_CALLBACK_URL", "")


class IntegrationSyncConfig:
    """Configuration for integration sync behavior."""

    INTEGRATION_SYNC_RETRY_ATTEMPTS: int = int(os.getenv("INTEGRATION_SYNC_RETRY_ATTEMPTS", "3"))
    INTEGRATION_MAX_CONCURRENT_SYNCS: int = int(os.getenv("INTEGRATION_MAX_CONCURRENT_SYNCS", "5"))


class GoogleIntegrationConfig:
    """Configuration for custom Google OAuth flows.

    Credentials are loaded from env vars (local dev) or Azure Key Vault
    (production) with a configurable TTL cache. The first call to
    get_credentials() fetches from Key Vault; subsequent calls return
    from memory until the TTL expires.
    """

    # Key Vault secret names
    SECRET_NAME_CLIENT_ID: str = "google-integration-client-id"
    SECRET_NAME_CLIENT_SECRET: str = "google-integration-client-secret"

    # Non-secret config — always from env
    REDIRECT_URI: str = os.getenv("GOOGLE_REDIRECT_URI", os.getenv("REDIRECT_URI", ""))
    GOOGLE_HTTP_TIMEOUT_SEC: float = float(os.getenv("GOOGLE_HTTP_TIMEOUT_SEC", "10"))

    # Cached credentials (populated from env or Key Vault)
    _client_id: str | None = os.getenv("GOOGLE_CLIENT_ID")
    _client_secret: str | None = os.getenv("GOOGLE_CLIENT_SECRET")
    _credentials_from_env: bool = bool(
        os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET")
    )
    _credentials_ttl_hours: float = float(os.getenv("GOOGLE_CREDENTIALS_TTL_HOURS", "6"))
    _credentials_loaded_at: float | None = None
    _refresh_lock: asyncio.Lock | None = None

    @property
    def google_client_id(self) -> str:
        """Sync access for backward compat — use get_credentials() in async code."""
        return self._client_id or ""

    @property
    def google_client_secret(self) -> str:
        """Sync access for backward compat — use get_credentials() in async code."""
        return self._client_secret or ""

    async def get_credentials(self) -> tuple[str, str]:
        """Return (client_id, client_secret), loading from Key Vault if needed.

        Env vars take precedence — when GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET
        are set, Key Vault is skipped entirely (for local development).
        """
        if self._credentials_from_env:
            return self._client_id or "", self._client_secret or ""

        if not self._is_expired():
            return self._client_id or "", self._client_secret or ""

        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        async with self._refresh_lock:
            if not self._is_expired():
                return self._client_id or "", self._client_secret or ""
            await self._load_from_vault()

        return self._client_id or "", self._client_secret or ""

    async def refresh_credentials(self) -> tuple[str, str]:
        """Force a Key Vault reload after Google rejects cached app credentials."""
        if self._credentials_from_env:
            return self._client_id or "", self._client_secret or ""

        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        async with self._refresh_lock:
            await self._load_from_vault()

        return self._client_id or "", self._client_secret or ""

    def _is_expired(self) -> bool:
        if self._credentials_loaded_at is None:
            return True
        import time

        elapsed_hours = (time.time() - self._credentials_loaded_at) / 3600
        return elapsed_hours >= self._credentials_ttl_hours

    async def _load_from_vault(self) -> None:
        import time

        from src.infra.secrets.manager import master_secrets_manager

        logger.info("Loading Google OAuth credentials from Key Vault (TTL refresh)")
        self._client_id = await master_secrets_manager.get(self.SECRET_NAME_CLIENT_ID)
        self._client_secret = await master_secrets_manager.get(self.SECRET_NAME_CLIENT_SECRET)
        self._credentials_loaded_at = time.time()

        if self._client_id and self._client_secret:
            logger.info(
                "Google OAuth credentials cached (TTL=%.1fh)",
                self._credentials_ttl_hours,
            )
        else:
            logger.error(
                "Google OAuth credentials missing from Key Vault (client_id=%s, client_secret=%s)",
                bool(self._client_id),
                bool(self._client_secret),
            )

    def validate(self, *, enabled: bool = True) -> None:
        """Fail fast when Google custom OAuth is enabled without core credentials.

        Only checks sync-available values (env vars or already-cached).
        For async validation, use get_credentials() which loads from Key Vault.
        """
        if not enabled:
            return

        missing = [
            name
            for name, value in {
                "GOOGLE_CLIENT_ID": self._client_id,
                "GOOGLE_CLIENT_SECRET": self._client_secret,
                "REDIRECT_URI": self.REDIRECT_URI,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Missing Google custom OAuth configuration: " + ", ".join(sorted(missing))
            )


class GoogleLoginConfig(GoogleIntegrationConfig):
    """Credentials for the Google provider configured in Supabase Auth.

    Login and Gmail/Calendar integrations intentionally have separate config
    names so production can rotate or replace either OAuth client independently.
    During rollout, ``GOOGLE_LOGIN_USE_INTEGRATION_CREDENTIALS=true`` aliases
    the values without making the integration secret names the login contract.
    """

    SECRET_NAME_CLIENT_ID = "google-login-client-id"
    SECRET_NAME_CLIENT_SECRET = "google-login-client-secret"

    def __init__(
        self,
        integration_config: GoogleIntegrationConfig,
        *,
        use_integration_credentials: bool | None = None,
    ) -> None:
        self._integration_config = integration_config
        self._client_id = (os.getenv("GOOGLE_LOGIN_CLIENT_ID") or "").strip() or None
        self._client_secret = (os.getenv("GOOGLE_LOGIN_CLIENT_SECRET") or "").strip() or None
        self._credentials_from_env = bool(self._client_id and self._client_secret)
        self._credentials_ttl_hours = float(os.getenv("GOOGLE_LOGIN_CREDENTIALS_TTL_HOURS", "6"))
        self._credentials_loaded_at = None
        self._refresh_lock = None
        environment = os.getenv("ENV", "dev").strip().lower()
        fallback_default = "false" if environment in {"prod", "production"} else "true"
        self._use_integration_credentials = (
            use_integration_credentials
            if use_integration_credentials is not None
            else os.getenv("GOOGLE_LOGIN_USE_INTEGRATION_CREDENTIALS", fallback_default).lower()
            == "true"
        )
        if environment in {"prod", "production"} and self._use_integration_credentials:
            raise RuntimeError("Production Google login must use dedicated credentials")

    async def get_credentials(self) -> tuple[str, str]:
        """Return the Google login OAuth pair for Supabase provider setup.

        Dedicated environment credentials always win. A partially configured
        pair is rejected instead of silently mixing login and integration apps.
        """
        self._validate_dedicated_env_pair()
        if self._credentials_from_env:
            return self._client_id or "", self._client_secret or ""
        if self._use_integration_credentials:
            return await self._integration_config.get_credentials()
        return await super().get_credentials()

    async def refresh_credentials(self) -> tuple[str, str]:
        """Refresh the configured source without crossing the credential boundary."""
        self._validate_dedicated_env_pair()
        if self._credentials_from_env:
            return self._client_id or "", self._client_secret or ""
        if self._use_integration_credentials:
            return await self._integration_config.refresh_credentials()
        return await super().refresh_credentials()

    def _validate_dedicated_env_pair(self) -> None:
        if bool(self._client_id) != bool(self._client_secret):
            raise RuntimeError(
                "GOOGLE_LOGIN_CLIENT_ID and GOOGLE_LOGIN_CLIENT_SECRET must be set together"
            )


class GoogleGmailPubSubConfig:
    """Configuration for Gmail Pub/Sub webhook authentication.

    The push token is a shared secret between our Pub/Sub subscription endpoint
    and the backend route. Env still wins for local testing, while deployed
    environments fetch the token from the master Key Vault and cache it.
    """

    SECRET_NAME_PUSH_TOKEN: str = os.getenv(
        "GOOGLE_GMAIL_PUBSUB_PUSH_TOKEN_SECRET_NAME",
        "google-gmail-pubsub-push-token",
    )
    SUBSCRIPTION: str = os.getenv("GOOGLE_GMAIL_PUBSUB_SUBSCRIPTION", "")

    _push_token: str | None = os.getenv("GOOGLE_GMAIL_PUBSUB_PUSH_TOKEN")
    _token_from_env: bool = bool(os.getenv("GOOGLE_GMAIL_PUBSUB_PUSH_TOKEN"))
    _credentials_ttl_hours: float = float(
        os.getenv("GOOGLE_GMAIL_PUBSUB_CREDENTIALS_TTL_HOURS", "6")
    )
    _credentials_loaded_at: float | None = None
    _refresh_lock: asyncio.Lock | None = None

    async def get_push_token(self) -> str | None:
        """Return the Gmail Pub/Sub push token, loading it from Key Vault if needed."""
        current_env_token = os.getenv("GOOGLE_GMAIL_PUBSUB_PUSH_TOKEN")
        if current_env_token:
            return current_env_token

        if self._token_from_env:
            return self._push_token

        if not self._is_expired():
            return self._push_token

        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        async with self._refresh_lock:
            if not self._is_expired():
                return self._push_token
            await self._load_from_vault()

        return self._push_token

    def _is_expired(self) -> bool:
        if self._credentials_loaded_at is None:
            return True
        import time

        elapsed_hours = (time.time() - self._credentials_loaded_at) / 3600
        return elapsed_hours >= self._credentials_ttl_hours

    async def _load_from_vault(self) -> None:
        import time

        from src.infra.secrets.manager import master_secrets_manager

        logger.info("Loading Gmail Pub/Sub push token from Key Vault (TTL refresh)")
        self._push_token = await master_secrets_manager.get(self.SECRET_NAME_PUSH_TOKEN)
        self._credentials_loaded_at = time.time()

        if self._push_token:
            logger.info(
                "Gmail Pub/Sub push token cached (TTL=%.1fh)",
                self._credentials_ttl_hours,
            )
        else:
            logger.error(
                "Gmail Pub/Sub push token missing from Key Vault secret '%s'",
                self.SECRET_NAME_PUSH_TOKEN,
            )


class MicrosoftIntegrationConfig:
    """Configuration for custom Microsoft OAuth flows.

    Credentials are loaded from env vars (local dev) or Azure Key Vault
    (production) with a configurable TTL cache — same pattern as
    GoogleIntegrationConfig.
    """

    # Key Vault secret names
    SECRET_NAME_CLIENT_ID: str = "microsoft-integration-client-id"
    SECRET_NAME_CLIENT_SECRET: str = "microsoft-integration-client-secret"

    # Non-secret config — always from env
    REDIRECT_URI: str = os.getenv("MICROSOFT_REDIRECT_URI", os.getenv("REDIRECT_URI", ""))
    MICROSOFT_HTTP_TIMEOUT_SEC: float = float(os.getenv("MICROSOFT_HTTP_TIMEOUT_SEC", "10"))

    # Cached credentials (populated from env or Key Vault)
    _client_id: str | None = os.getenv("MICROSOFT_CLIENT_ID")
    _client_secret: str | None = os.getenv("MICROSOFT_CLIENT_SECRET")
    _credentials_from_env: bool = bool(
        os.getenv("MICROSOFT_CLIENT_ID") and os.getenv("MICROSOFT_CLIENT_SECRET")
    )
    _credentials_ttl_hours: float = float(os.getenv("MICROSOFT_CREDENTIALS_TTL_HOURS", "6"))
    _credentials_loaded_at: float | None = None
    _refresh_lock: asyncio.Lock | None = None

    @property
    def microsoft_client_id(self) -> str:
        """Sync access for backward compat — use get_credentials() in async code."""
        return self._client_id or ""

    @property
    def microsoft_client_secret(self) -> str:
        """Sync access for backward compat — use get_credentials() in async code."""
        return self._client_secret or ""

    async def get_credentials(self) -> tuple[str, str]:
        """Return (client_id, client_secret), loading from Key Vault if needed.

        Env vars take precedence — when MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET
        are set, Key Vault is skipped entirely (for local development).
        """
        if self._credentials_from_env:
            return self._client_id or "", self._client_secret or ""

        if not self._is_expired():
            return self._client_id or "", self._client_secret or ""

        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        async with self._refresh_lock:
            if not self._is_expired():
                return self._client_id or "", self._client_secret or ""
            await self._load_from_vault()

        return self._client_id or "", self._client_secret or ""

    def _is_expired(self) -> bool:
        if self._credentials_loaded_at is None:
            return True
        import time

        elapsed_hours = (time.time() - self._credentials_loaded_at) / 3600
        return elapsed_hours >= self._credentials_ttl_hours

    async def _load_from_vault(self) -> None:
        import time

        from src.infra.secrets.manager import master_secrets_manager

        logger.info("Loading Microsoft OAuth credentials from Key Vault (TTL refresh)")
        self._client_id = await master_secrets_manager.get(self.SECRET_NAME_CLIENT_ID)
        self._client_secret = await master_secrets_manager.get(self.SECRET_NAME_CLIENT_SECRET)
        self._credentials_loaded_at = time.time()

        if self._client_id and self._client_secret:
            logger.info(
                "Microsoft OAuth credentials cached (TTL=%.1fh)",
                self._credentials_ttl_hours,
            )
        else:
            logger.error(
                "Microsoft OAuth credentials missing from Key Vault "
                "(client_id=%s, client_secret=%s)",
                bool(self._client_id),
                bool(self._client_secret),
            )

    def validate(self, *, enabled: bool = True) -> None:
        """Fail fast when Microsoft custom OAuth is enabled without core credentials."""
        if not enabled:
            return

        missing = [
            name
            for name, value in {
                "MICROSOFT_CLIENT_ID": self._client_id,
                "MICROSOFT_CLIENT_SECRET": self._client_secret,
                "REDIRECT_URI": self.REDIRECT_URI,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Missing Microsoft custom OAuth configuration: " + ", ".join(sorted(missing))
            )


class HubSpotIntegrationConfig:
    """Configuration for custom HubSpot OAuth flows.

    Credentials are loaded from env vars (local dev) or Azure Key Vault
    (production) with a configurable TTL cache — same pattern as
    GoogleIntegrationConfig and MicrosoftIntegrationConfig.
    """

    # Key Vault secret names
    SECRET_NAME_CLIENT_ID: str = "hubspot-integration-client-id"
    SECRET_NAME_CLIENT_SECRET: str = "hubspot-integration-client-secret"

    # Non-secret config — always from env
    REDIRECT_URI: str = os.getenv("HUBSPOT_REDIRECT_URI", os.getenv("REDIRECT_URI", ""))
    HUBSPOT_HTTP_TIMEOUT_SEC: float = float(os.getenv("HUBSPOT_HTTP_TIMEOUT_SEC", "10"))
    WEBHOOK_PUBLIC_URL: str = os.getenv("HUBSPOT_WEBHOOK_PUBLIC_URL", "").strip()

    # Cached credentials (populated from env or Key Vault)
    _client_id: str | None = os.getenv("HUBSPOT_CLIENT_ID")
    _client_secret: str | None = os.getenv("HUBSPOT_CLIENT_SECRET")
    _credentials_from_env: bool = bool(
        os.getenv("HUBSPOT_CLIENT_ID") and os.getenv("HUBSPOT_CLIENT_SECRET")
    )
    _credentials_ttl_hours: float = float(os.getenv("HUBSPOT_CREDENTIALS_TTL_HOURS", "6"))
    _credentials_loaded_at: float | None = None
    _refresh_lock: asyncio.Lock | None = None

    @property
    def hubspot_client_id(self) -> str:
        """Sync access for backward compat — use get_credentials() in async code."""
        return self._client_id or ""

    @property
    def hubspot_client_secret(self) -> str:
        """Sync access for backward compat — use get_credentials() in async code."""
        return self._client_secret or ""

    async def get_credentials(self) -> tuple[str, str]:
        """Return (client_id, client_secret), loading from Key Vault if needed.

        Env vars take precedence — when HUBSPOT_CLIENT_ID and
        HUBSPOT_CLIENT_SECRET are set, Key Vault is skipped entirely.
        """
        if self._credentials_from_env:
            return self._client_id or "", self._client_secret or ""

        if not self._is_expired():
            return self._client_id or "", self._client_secret or ""

        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        async with self._refresh_lock:
            if not self._is_expired():
                return self._client_id or "", self._client_secret or ""
            await self._load_from_vault()

        return self._client_id or "", self._client_secret or ""

    def _is_expired(self) -> bool:
        if self._credentials_loaded_at is None:
            return True
        import time

        elapsed_hours = (time.time() - self._credentials_loaded_at) / 3600
        return elapsed_hours >= self._credentials_ttl_hours

    async def _load_from_vault(self) -> None:
        import time

        from src.infra.secrets.manager import master_secrets_manager

        logger.info("Loading HubSpot OAuth credentials from Key Vault (TTL refresh)")
        self._client_id = await master_secrets_manager.get(self.SECRET_NAME_CLIENT_ID)
        self._client_secret = await master_secrets_manager.get(self.SECRET_NAME_CLIENT_SECRET)
        self._credentials_loaded_at = time.time()

        if self._client_id and self._client_secret:
            logger.info(
                "HubSpot OAuth credentials cached (TTL=%.1fh)",
                self._credentials_ttl_hours,
            )
        else:
            logger.error(
                "HubSpot OAuth credentials missing from Key Vault (client_id=%s, client_secret=%s)",
                bool(self._client_id),
                bool(self._client_secret),
            )

    def validate(self, *, enabled: bool = True) -> None:
        """Fail fast when HubSpot custom OAuth is enabled without core credentials."""
        if not enabled:
            return

        missing = [
            name
            for name, value in {
                "HUBSPOT_CLIENT_ID": self._client_id,
                "HUBSPOT_CLIENT_SECRET": self._client_secret,
                "HUBSPOT_REDIRECT_URI": self.REDIRECT_URI,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Missing HubSpot custom OAuth configuration: " + ", ".join(sorted(missing))
            )


class SalesforceIntegrationConfig:
    """Configuration for custom Salesforce OAuth flows.

    Credentials are loaded from env vars (local dev) or Azure Key Vault
    (production) with a configurable TTL cache — same pattern as the
    other first-party custom OAuth providers.
    """

    SECRET_NAME_CLIENT_ID: str = "salesforce-integration-client-id"
    SECRET_NAME_CLIENT_SECRET: str = "salesforce-integration-client-secret"

    REDIRECT_URI: str = os.getenv("SALESFORCE_REDIRECT_URI", os.getenv("REDIRECT_URI", ""))
    SALESFORCE_HTTP_TIMEOUT_SEC: float = float(os.getenv("SALESFORCE_HTTP_TIMEOUT_SEC", "10"))
    EXTERNAL_CLIENT_APP_INSTALL_URL: str = os.getenv(
        "SALESFORCE_EXTERNAL_CLIENT_APP_INSTALL_URL", ""
    )

    _client_id: str | None = os.getenv("SALESFORCE_CLIENT_ID")
    _client_secret: str | None = os.getenv("SALESFORCE_CLIENT_SECRET")
    _credentials_from_env: bool = bool(
        os.getenv("SALESFORCE_CLIENT_ID") and os.getenv("SALESFORCE_CLIENT_SECRET")
    )
    _credentials_ttl_hours: float = float(os.getenv("SALESFORCE_CREDENTIALS_TTL_HOURS", "6"))
    _credentials_loaded_at: float | None = None
    _refresh_lock: asyncio.Lock | None = None

    def __init__(self) -> None:
        self.EXTERNAL_CLIENT_APP_INSTALL_URL = os.getenv(
            "SALESFORCE_EXTERNAL_CLIENT_APP_INSTALL_URL", ""
        )

    @property
    def salesforce_client_id(self) -> str:
        """Sync access for backward compat — use get_credentials() in async code."""
        return self._client_id or ""

    @property
    def salesforce_client_secret(self) -> str:
        """Sync access for backward compat — use get_credentials() in async code."""
        return self._client_secret or ""

    async def get_credentials(self) -> tuple[str, str]:
        """Return (client_id, client_secret), loading from Key Vault if needed."""
        if self._credentials_from_env:
            return self._client_id or "", self._client_secret or ""

        if not self._is_expired():
            return self._client_id or "", self._client_secret or ""

        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        async with self._refresh_lock:
            if not self._is_expired():
                return self._client_id or "", self._client_secret or ""
            await self._load_from_vault()

        return self._client_id or "", self._client_secret or ""

    def _is_expired(self) -> bool:
        if self._credentials_loaded_at is None:
            return True
        import time

        elapsed_hours = (time.time() - self._credentials_loaded_at) / 3600
        return elapsed_hours >= self._credentials_ttl_hours

    async def _load_from_vault(self) -> None:
        import time

        from src.infra.secrets.manager import master_secrets_manager

        logger.info("Loading Salesforce OAuth credentials from Key Vault (TTL refresh)")
        self._client_id = await master_secrets_manager.get(self.SECRET_NAME_CLIENT_ID)
        self._client_secret = await master_secrets_manager.get(self.SECRET_NAME_CLIENT_SECRET)
        self._credentials_loaded_at = time.time()

        if self._client_id and self._client_secret:
            logger.info(
                "Salesforce OAuth credentials cached (TTL=%.1fh)",
                self._credentials_ttl_hours,
            )
        else:
            logger.error(
                "Salesforce OAuth credentials missing from Key Vault "
                "(client_id=%s, client_secret=%s)",
                bool(self._client_id),
                bool(self._client_secret),
            )

    def validate(self, *, enabled: bool = True) -> None:
        """Fail fast when Salesforce custom OAuth is enabled without core credentials."""
        if not enabled:
            return

        missing = [
            name
            for name, value in {
                "SALESFORCE_CLIENT_ID": self._client_id,
                "SALESFORCE_CLIENT_SECRET": self._client_secret,
                "SALESFORCE_REDIRECT_URI": self.REDIRECT_URI,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Missing Salesforce custom OAuth configuration: " + ", ".join(sorted(missing))
            )


class SecretManagerConfig:
    """Configuration for Cloud Secret Manager"""

    # Primary Key Vault for org-specific secrets (DB passwords per tenant)
    AZURE_KEY_VAULT_URL: str | None = os.getenv("AZURE_KEY_VAULT_URL")

    # Master Key Vault for global/shared secrets (Teams bot, API keys, etc.)
    # Falls back to AZURE_KEY_VAULT_URL if not set
    AZURE_MASTER_KEY_VAULT_URL: str | None = os.getenv(
        "AZURE_MASTER_KEY_VAULT_URL", os.getenv("AZURE_KEY_VAULT_URL")
    )

    # Bound individual secret fetch duration to avoid event-loop stalls while
    # still allowing Azure Key Vault enough time to answer during transient slowness.
    SECRET_GET_TIMEOUT_SEC: int = int(os.getenv("SECRET_GET_TIMEOUT_SEC", "20"))
    SECRET_GET_MAX_RETRIES: int = int(os.getenv("SECRET_GET_MAX_RETRIES", "2"))
    SECRET_GET_RETRY_INITIAL_DELAY_SEC: float = float(
        os.getenv("SECRET_GET_RETRY_INITIAL_DELAY_SEC", "0.5")
    )
    SECRET_GET_RETRY_MAX_DELAY_SEC: float = float(
        os.getenv("SECRET_GET_RETRY_MAX_DELAY_SEC", "4.0")
    )


class CloudInfraConfig:
    """Configuration for Cloud Infrastructure"""

    AZURE_SUBSCRIPTION_ID: str | None = os.getenv("AZURE_SUBSCRIPTION_ID")
    AZURE_RESOURCE_GROUP: str | None = os.getenv("AZURE_RESOURCE_GROUP")
    # Optional auth parameters (may be absent when relying purely on workload identity)
    AZURE_TENANT_ID: str | None = os.getenv("AZURE_TENANT_ID")
    AZURE_CLIENT_ID: str | None = os.getenv("AZURE_CLIENT_ID")
    AZURE_CLIENT_SECRET: str | None = os.getenv("AZURE_CLIENT_SECRET")
    AZURE_ADDITIONALLY_ALLOWED_TENANTS: str | None = os.getenv("AZURE_ADDITIONALLY_ALLOWED_TENANTS")


class ChatFileUploadConfig:
    """Configuration for file uploads."""

    # Max file size in bytes (e.g., 50 * 1024 * 1024 for 50MB)
    MAX_FILE_SIZE_BYTES: int = int(os.getenv("MAX_UPLOAD_FILE_SIZE_BYTES", "52428800"))

    # Allowed MIME types for chat attachment uploads
    _DEFAULT_CHAT_MIMETYPES = (
        # Documents
        "application/pdf, "
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document, "
        "application/vnd.openxmlformats-officedocument.presentationml.presentation, "
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, "
        # Text formats
        "text/plain, text/csv, text/tab-separated-values, text/tsv, "
        "text/markdown, text/x-markdown, "
        # Data formats
        "application/x-ndjson, application/ndjson, application/jsonl, application/json, "
        # Images
        "image/png, image/jpeg, image/gif, image/webp, "
        # Audio
        "audio/mpeg, audio/mp3, audio/wav, audio/ogg, audio/webm, "
        "audio/mp4, audio/m4a, audio/x-m4a, audio/aac, audio/amr"
    )

    ALLOWED_MIMETYPES: list[str] = [
        item.strip()
        for item in os.getenv("ALLOWED_MIMETYPES", _DEFAULT_CHAT_MIMETYPES).split(",")
        if item.strip()
    ]
    BOT_ATTACHMENT_DOWNLOAD_TIMEOUT_SECONDS: int = int(
        os.getenv("BOT_ATTACHMENT_DOWNLOAD_TIMEOUT_SECONDS", "60")
    )
    BOT_ATTACHMENT_MAX_INGEST_BYTES: int = int(
        os.getenv("BOT_ATTACHMENT_MAX_INGEST_BYTES", str(MAX_FILE_SIZE_BYTES))
    )
    ATTACHMENT_MANIFEST_MAX_ITEMS: int = int(os.getenv("CHAT_ATTACHMENT_MANIFEST_MAX_ITEMS", "10"))
    ATTACHMENT_READ_MAX_CHARS: int = int(os.getenv("CHAT_ATTACHMENT_READ_MAX_CHARS", "12000"))
    # Automatic grounding is deliberately smaller than upload limits. Larger or
    # older content remains available through read_chat_attachment.
    ATTACHMENT_AUTO_READ_MAX_ITEMS: int = int(os.getenv("CHAT_ATTACHMENT_AUTO_READ_MAX_ITEMS", "3"))
    ATTACHMENT_AUTO_READ_MAX_TOKENS: int = int(
        os.getenv("CHAT_ATTACHMENT_AUTO_READ_MAX_TOKENS", "3000")
    )
    ATTACHMENT_RETENTION_DAYS: int = int(os.getenv("CHAT_ATTACHMENT_RETENTION_DAYS", "30"))


class KnowledgeFileUploadConfig:
    """Configuration for knowledge base file uploads.

    Uses distinct environment variable names to allow independent tuning
    from chat attachment uploads.
    """

    MAX_FILE_SIZE_BYTES: int = int(
        os.getenv(
            "KNOWLEDGE_MAX_UPLOAD_FILE_SIZE_BYTES",
            os.getenv("MAX_UPLOAD_FILE_SIZE_BYTES", "104857600"),
        )
    )  # default 100MB if not set

    # Default MIME types aligned with knowledge worker supported formats
    _DEFAULT_KNOWLEDGE_MIMETYPES = (
        # Documents
        "application/pdf, "
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document, "  # DOCX
        "application/vnd.openxmlformats-officedocument.presentationml.presentation, "  # PPTX
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, "  # XLSX
        # Text formats
        "text/plain, "
        "text/markdown, "
        "text/x-markdown, "
        "text/csv, "
        "text/html, "
        # Data formats
        "application/json, "
        "application/xml, "
        "text/xml, "
        # Images (for OCR processing)
        "image/jpeg, "
        "image/png, "
        "image/webp, "
        "image/gif"
    )

    ALLOWED_MIMETYPES: list[str] = [
        item.strip()
        for item in os.getenv(
            "KNOWLEDGE_ALLOWED_MIMETYPES",
            _DEFAULT_KNOWLEDGE_MIMETYPES,
        ).split(",")
        if item.strip()
    ]


class ThumbnailConfig:
    """Configuration for thumbnail generation behavior."""

    ENABLE_TEXT_PLACEHOLDERS: bool = os.getenv("ENABLE_TEXT_PLACEHOLDERS", "true").lower() == "true"
    ENABLE_PDF_PLACEHOLDER_FALLBACK: bool = (
        os.getenv("ENABLE_PDF_PLACEHOLDER_FALLBACK", "false").lower() == "true"
    )

    # Explicitly list the MIME types / families the thumbnail pipeline supports.
    # This centralises what thumbnail generation will attempt so other parts of the
    # codebase (validation, UI hints) can reference a single source of truth.
    # Explicit safe raster image MIME types we actively thumbnail. Excludes SVG (vector) and
    # very large/heavy formats by default for simplicity & security. Can be overridden via env.
    IMAGE_MIMETYPES: list[str] = [
        item.strip()
        for item in os.getenv(
            "THUMBNAIL_IMAGE_MIMETYPES",
            "image/jpeg,image/png,image/webp,image/gif",
        ).split(",")
        if item.strip()
    ]
    # Keep MIME handling explicit by default; enable broad image/* matching only when the
    # environment explicitly opts into fallback handling.
    IMAGE_PREFIXES: list[str] = ["image/"]
    ENABLE_IMAGE_PREFIX_FALLBACK: bool = (
        os.getenv("THUMBNAIL_ENABLE_IMAGE_PREFIX_FALLBACK", "false").lower() == "true"
    )
    PDF_MIMETYPES: list[str] = ["application/pdf"]
    TEXT_LABEL_MAP: dict[str, str] = {
        # Plain text & structured line-oriented formats
        "text/plain": "TXT",
        "text/csv": "CSV",
        "text/tab-separated-values": "TSV",
        "text/tsv": "TSV",
        # Markup / documentation
        "text/markdown": "MD",
        "text/x-markdown": "MD",
        # Streaming / line-delimited JSON
        "application/x-ndjson": "NDJSON",
        "application/ndjson": "NDJSON",
        "application/jsonl": "JSONL",
        # Regular JSON
        "application/json": "JSON",
    }

    # Office / document formats we can safely represent via labeled placeholders.
    # (We avoid attempting to fully render content pages to keep infra lightweight.)
    DOCUMENT_LABEL_MAP: dict[str, str] = {
        "application/msword": "DOC",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "DOCX",
        "application/vnd.ms-powerpoint": "PPT",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PPTX",
        "application/vnd.ms-excel": "XLS",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "XLSX",
        # OpenDocument formats
        "application/vnd.oasis.opendocument.text": "ODT",
        "application/vnd.oasis.opendocument.spreadsheet": "ODS",
        "application/vnd.oasis.opendocument.presentation": "ODP",
        # Markup & structured docs (rendered only as placeholder, never executed)
        "text/html": "HTML",
        "application/xml": "XML",
        "text/xml": "XML",
        "application/x-yaml": "YAML",
        "text/yaml": "YAML",
        "text/x-yaml": "YAML",
    }

    @classmethod
    def all_supported_mimetypes(cls) -> set[str]:
        """Aggregate every allowed MIME type for central validation and UI hints."""
        return (
            set(cls.PDF_MIMETYPES)
            | set(cls.TEXT_LABEL_MAP.keys())
            | set(cls.DOCUMENT_LABEL_MAP.keys())
        )

    @classmethod
    def is_supported(cls, mimetype: str) -> bool:
        """Return whether a MIME type is supported for thumbnail generation."""
        if not mimetype:
            return False
        if mimetype in cls.IMAGE_MIMETYPES:
            return True
        if cls.ENABLE_IMAGE_PREFIX_FALLBACK and any(
            mimetype.startswith(pref) for pref in cls.IMAGE_PREFIXES
        ):
            return True
        if mimetype in cls.PDF_MIMETYPES:
            return True
        return bool(mimetype in cls.TEXT_LABEL_MAP or mimetype in cls.DOCUMENT_LABEL_MAP)


thumbnail_config = ThumbnailConfig()


class RateLimitConfig:
    """Configuration for API rate limiting.

    Defaults are conservative for testing; override via env vars when needed.
    """

    PER_MINUTE: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "120"))
    PER_HOUR: int = int(os.getenv("RATE_LIMIT_PER_HOUR", "2000"))

    # Lenient limits for read-only (GET) endpoints
    READ_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_READ_PER_MINUTE", "120"))
    READ_PER_HOUR: int = int(os.getenv("RATE_LIMIT_READ_PER_HOUR", "2000"))

    # Contact research reaches a paid external provider, so it needs a much
    # smaller budget than ordinary read-only API traffic.
    CONTACT_RESEARCH_PER_MINUTE: int = int(
        os.getenv("RATE_LIMIT_CONTACT_RESEARCH_PER_MINUTE", "10")
    )
    CONTACT_RESEARCH_PER_HOUR: int = int(os.getenv("RATE_LIMIT_CONTACT_RESEARCH_PER_HOUR", "100"))

    # Public lead forms need more headroom because anonymous website traffic is
    # scoped per form and client IP, not per authenticated user.
    PUBLIC_FORM_READ_PER_MINUTE: int = int(
        os.getenv("RATE_LIMIT_PUBLIC_FORM_READ_PER_MINUTE", "240")
    )
    PUBLIC_FORM_READ_PER_HOUR: int = int(os.getenv("RATE_LIMIT_PUBLIC_FORM_READ_PER_HOUR", "5000"))
    PUBLIC_FORM_SUBMIT_PER_MINUTE: int = int(
        os.getenv("RATE_LIMIT_PUBLIC_FORM_SUBMIT_PER_MINUTE", "180")
    )
    PUBLIC_FORM_SUBMIT_PER_HOUR: int = int(
        os.getenv("RATE_LIMIT_PUBLIC_FORM_SUBMIT_PER_HOUR", "3000")
    )
    PUBLIC_FORM_SUBMIT_PER_DAY: int = int(os.getenv("RATE_LIMIT_PUBLIC_FORM_SUBMIT_PER_DAY", "100"))
    PRIVATE_LEAD_INGESTION_PER_MINUTE: int = int(
        os.getenv("RATE_LIMIT_PRIVATE_LEAD_INGESTION_PER_MINUTE", "300")
    )
    PRIVATE_LEAD_INGESTION_PER_HOUR: int = int(
        os.getenv("RATE_LIMIT_PRIVATE_LEAD_INGESTION_PER_HOUR", "10000")
    )
    TRUST_EXTERNAL_PROXY_HEADERS: bool = (
        os.getenv("TRUST_EXTERNAL_PROXY_HEADERS", "false").lower() == "true"
    )

    # Strict limits for authentication (login attempts) - per IP+email
    AUTH_LOGIN_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_AUTH_LOGIN_PER_MINUTE", "50"))
    AUTH_LOGIN_PER_HOUR: int = int(os.getenv("RATE_LIMIT_AUTH_LOGIN_PER_HOUR", "100"))

    # Organisation signup grants admin authority; writes are strict while status
    # reads permit ordinary frontend polling during asynchronous provisioning.
    AUTH_SIGNUP_IP_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_AUTH_SIGNUP_IP_PER_MINUTE", "20"))
    AUTH_SIGNUP_IP_PER_HOUR: int = int(os.getenv("RATE_LIMIT_AUTH_SIGNUP_IP_PER_HOUR", "100"))
    AUTH_SIGNUP_IDENTITY_PER_MINUTE: int = int(
        os.getenv("RATE_LIMIT_AUTH_SIGNUP_IDENTITY_PER_MINUTE", "5")
    )
    AUTH_SIGNUP_IDENTITY_PER_HOUR: int = int(
        os.getenv("RATE_LIMIT_AUTH_SIGNUP_IDENTITY_PER_HOUR", "20")
    )
    AUTH_SIGNUP_STATUS_IP_PER_MINUTE: int = int(
        os.getenv("RATE_LIMIT_AUTH_SIGNUP_STATUS_IP_PER_MINUTE", "120")
    )
    AUTH_SIGNUP_STATUS_IP_PER_HOUR: int = int(
        os.getenv("RATE_LIMIT_AUTH_SIGNUP_STATUS_IP_PER_HOUR", "2000")
    )
    AUTH_SIGNUP_STATUS_PER_MINUTE: int = int(
        os.getenv("RATE_LIMIT_AUTH_SIGNUP_STATUS_PER_MINUTE", "60")
    )
    AUTH_SIGNUP_STATUS_PER_HOUR: int = int(
        os.getenv("RATE_LIMIT_AUTH_SIGNUP_STATUS_PER_HOUR", "600")
    )

    # IP-only total limit for login (prevents credential stuffing across many emails)
    LOGIN_IP_TOTAL_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_LOGIN_IP_TOTAL_PER_MINUTE", "100"))
    LOGIN_IP_TOTAL_PER_HOUR: int = int(os.getenv("RATE_LIMIT_LOGIN_IP_TOTAL_PER_HOUR", "300"))


# Instantiate config objects once so all code paths share stateful config instances.
supabase_config = SupabaseConfig()
database_config = DatabaseConfig()
app_config = AppConfig()
security_secrets_config = SecuritySecretsConfig()
openai_config = OpenAIConfig()
embedder_config = EmbedderConfig()
transcription_config = TranscriptionConfig()
cloud_storage_config = CloudStorageConfig()
resend_config = ResendConfig()
super_admin_config = SuperAdminConfig()
secret_manager_config = SecretManagerConfig()
cloud_infra_config = CloudInfraConfig()
chat_file_upload_config = ChatFileUploadConfig()
knowledge_file_upload_config = KnowledgeFileUploadConfig()
# NOTE: thumbnail_config now sourced from thumbnail_config.py for modularity.
rate_limit_config = RateLimitConfig()
integration_sync_config = IntegrationSyncConfig()
google_integration_config = GoogleIntegrationConfig()
google_login_config = GoogleLoginConfig(google_integration_config)
google_gmail_pubsub_config = GoogleGmailPubSubConfig()
microsoft_integration_config = MicrosoftIntegrationConfig()
hubspot_integration_config = HubSpotIntegrationConfig()
salesforce_integration_config = SalesforceIntegrationConfig()
apollo_config = ApolloConfig()
exa_config = ExaConfig()
recall_config = RecallConfig()
teams_config = TeamsConfig()


class SlackConfig:
    """Configuration for Slack Bot integration.

    Bot credentials are stored in Azure Key Vault (master vault) and cached
    with a configurable TTL (default 6 hours).

    Required Key Vault secrets:
    - slack-bot-token: Bot User OAuth Token (xoxb-...)
    - slack-signing-secret: Slack app signing secret for request verification
    - slack-app-id: Slack application ID
    """

    SLACK_ENABLED: bool = os.getenv("SLACK_ENABLED", "true").lower() == "true"

    # Key Vault secret names
    SECRET_NAME_SIGNING_SECRET: str = "slack-signing-secret"
    SECRET_NAME_APP_ID: str = "slack-app-id"
    SECRET_NAME_CLIENT_ID: str = "slack-client-id"
    SECRET_NAME_CLIENT_SECRET: str = "slack-client-secret"

    # Cached credential values (global app-level, not per-workspace)
    SIGNING_SECRET: str | None = None
    APP_ID: str | None = None
    CLIENT_ID: str | None = None
    CLIENT_SECRET: str | None = None

    # TTL for cached credentials (hours)
    _credentials_ttl_hours: float = float(os.getenv("SLACK_BOT_CREDENTIALS_TTL_HOURS", "6"))
    _credentials_loaded_at: float | None = None
    _refresh_lock: asyncio.Lock | None = None

    async def get_credentials(self) -> tuple[str | None, str | None, str | None, str | None]:
        """Return cached app credentials, refreshing from Key Vault if TTL expired.

        These are global app-level credentials (signing secret, client ID/secret).
        Per-workspace bot tokens are stored in bot_platform_installations table.

        Returns:
            Tuple of (signing_secret, app_id, client_id, client_secret).
        """
        if not self._is_expired():
            return self.SIGNING_SECRET, self.APP_ID, self.CLIENT_ID, self.CLIENT_SECRET

        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        async with self._refresh_lock:
            if not self._is_expired():
                return self.SIGNING_SECRET, self.APP_ID, self.CLIENT_ID, self.CLIENT_SECRET
            await self._load_credentials_from_vault()

        return self.SIGNING_SECRET, self.APP_ID, self.CLIENT_ID, self.CLIENT_SECRET

    def _is_expired(self) -> bool:
        if self._credentials_loaded_at is None:
            return True
        import time

        elapsed_hours = (time.time() - self._credentials_loaded_at) / 3600
        return elapsed_hours >= self._credentials_ttl_hours

    async def _load_credentials_from_vault(self) -> None:
        import time

        from src.infra.secrets.manager import master_secrets_manager

        logger.info("Loading Slack app credentials from Key Vault (TTL refresh)")
        self.SIGNING_SECRET = await master_secrets_manager.get(self.SECRET_NAME_SIGNING_SECRET)
        self.APP_ID = await master_secrets_manager.get(self.SECRET_NAME_APP_ID)
        self.CLIENT_ID = await master_secrets_manager.get(self.SECRET_NAME_CLIENT_ID)
        self.CLIENT_SECRET = await master_secrets_manager.get(self.SECRET_NAME_CLIENT_SECRET)
        self._credentials_loaded_at = time.time()

        if self.SIGNING_SECRET and self.CLIENT_ID:
            logger.info("Slack app credentials cached (TTL=%.1fh)", self._credentials_ttl_hours)
        else:
            logger.error("Slack app credentials missing from Key Vault")

    # Webhook deduplication
    SLACK_WEBHOOK_DEDUPE_TTL_SECONDS: int = int(
        os.getenv("SLACK_WEBHOOK_DEDUPE_TTL_SECONDS", "300")
    )

    # OAuth callback URL for the user-level Slack integration flow.
    # Mirrors Google/Microsoft so local frontend APP_URL does not leak into
    # provider redirects for backend callback endpoints.
    REDIRECT_URI: str = os.getenv("SLACK_REDIRECT_URI", os.getenv("REDIRECT_URI", ""))

    # OAuth callback URL for the workspace-level Slack bot install flow.
    SLACK_BOT_REDIRECT_URI: str = os.getenv("SLACK_BOT_REDIRECT_URI", REDIRECT_URI)

    # Auth URLs for the login flow
    SLACK_AUTH_LOGIN_URL: str = os.getenv(
        "SLACK_AUTH_LOGIN_URL",
        f"{os.getenv('APP_URL', '').rstrip('/')}/bots/auth" if os.getenv("APP_URL") else "",
    )


class WhatsAppConfig:
    """Configuration for WhatsApp Business Platform bot integration.

    Credentials are loaded from env vars for local/dev usage or from the master
    Key Vault in shared environments. The webhook verify token is the shared
    secret pasted into Meta's webhook configuration; Meta does not issue it.
    """

    WHATSAPP_ENABLED: bool = os.getenv("WHATSAPP_ENABLED", "true").lower() == "true"

    SECRET_NAME_ACCESS_TOKEN: str = "whatsapp-access-token"
    SECRET_NAME_WEBHOOK_VERIFY_TOKEN: str = "whatsapp-verify-token"
    SECRET_NAME_APP_SECRET: str = "whatsapp-app-secret"

    ACCESS_TOKEN: str | None = os.getenv("WHATSAPP_ACCESS_TOKEN")
    WEBHOOK_VERIFY_TOKEN: str | None = os.getenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN")
    APP_SECRET: str | None = os.getenv("WHATSAPP_APP_SECRET")

    _access_token_from_env: bool = bool(os.getenv("WHATSAPP_ACCESS_TOKEN"))
    _webhook_verify_token_from_env: bool = bool(os.getenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN"))
    _app_secret_from_env: bool = bool(os.getenv("WHATSAPP_APP_SECRET"))
    _credentials_ttl_hours: float = float(os.getenv("WHATSAPP_CREDENTIALS_TTL_HOURS", "6"))
    _credentials_loaded_at: float | None = None
    _refresh_lock: asyncio.Lock | None = None

    GRAPH_API_VERSION: str = os.getenv("WHATSAPP_GRAPH_API_VERSION", "v25.0")
    WEBHOOK_DEDUPE_TTL_SECONDS: int = int(os.getenv("WHATSAPP_WEBHOOK_DEDUPE_TTL_SECONDS", "300"))
    WHATSAPP_BOT_PHONE_NUMBER: str = os.getenv("WHATSAPP_BOT_PHONE_NUMBER", "")
    WHATSAPP_AUTH_LOGIN_URL: str = os.getenv(
        "WHATSAPP_AUTH_LOGIN_URL",
        f"{os.getenv('APP_URL', '').rstrip('/')}/bots/auth" if os.getenv("APP_URL") else "",
    )

    async def get_credentials(self) -> tuple[str | None, str | None, str | None]:
        """Return access token, webhook verify token, and app secret."""
        if self._all_credentials_from_env():
            return self.ACCESS_TOKEN, self.WEBHOOK_VERIFY_TOKEN, self.APP_SECRET

        if not self._is_expired():
            return self.ACCESS_TOKEN, self.WEBHOOK_VERIFY_TOKEN, self.APP_SECRET

        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        async with self._refresh_lock:
            if not self._is_expired():
                return self.ACCESS_TOKEN, self.WEBHOOK_VERIFY_TOKEN, self.APP_SECRET
            await self._load_credentials_from_vault()

        return self.ACCESS_TOKEN, self.WEBHOOK_VERIFY_TOKEN, self.APP_SECRET

    async def get_webhook_verify_token(self) -> str | None:
        """Return only the token needed for Meta webhook challenge verification."""
        if self._webhook_verify_token_from_env:
            return self.WEBHOOK_VERIFY_TOKEN

        await self.get_credentials()
        return self.WEBHOOK_VERIFY_TOKEN

    async def get_access_token(self) -> str | None:
        """Return the token used for outbound WhatsApp Cloud API calls."""
        if self._access_token_from_env:
            return self.ACCESS_TOKEN

        await self.get_credentials()
        return self.ACCESS_TOKEN

    async def get_app_secret(self) -> str | None:
        """Return the Meta app secret used to validate signed webhook POSTs."""
        if self._app_secret_from_env:
            return self.APP_SECRET

        await self.get_credentials()
        return self.APP_SECRET

    def _all_credentials_from_env(self) -> bool:
        return (
            self._access_token_from_env
            and self._webhook_verify_token_from_env
            and self._app_secret_from_env
        )

    def _is_expired(self) -> bool:
        if self._credentials_loaded_at is None:
            return True
        import time

        elapsed_hours = (time.time() - self._credentials_loaded_at) / 3600
        return elapsed_hours >= self._credentials_ttl_hours

    async def _load_credentials_from_vault(self) -> None:
        import time

        from src.infra.secrets.manager import master_secrets_manager

        logger.info("Loading WhatsApp app credentials from Key Vault (TTL refresh)")
        if not self._access_token_from_env:
            self.ACCESS_TOKEN = await master_secrets_manager.get(self.SECRET_NAME_ACCESS_TOKEN)
        if not self._webhook_verify_token_from_env:
            self.WEBHOOK_VERIFY_TOKEN = await master_secrets_manager.get(
                self.SECRET_NAME_WEBHOOK_VERIFY_TOKEN
            )
        if not self._app_secret_from_env:
            self.APP_SECRET = await master_secrets_manager.get(self.SECRET_NAME_APP_SECRET)

        self._credentials_loaded_at = time.time()

        if self.ACCESS_TOKEN and self.WEBHOOK_VERIFY_TOKEN and self.APP_SECRET:
            logger.info("WhatsApp app credentials cached (TTL=%.1fh)", self._credentials_ttl_hours)
        else:
            logger.error("WhatsApp app credentials missing from Key Vault")


class GoogleChatConfig:
    """Configuration for Google Chat app bot integration."""

    GOOGLE_CHAT_ENABLED: bool = True
    GOOGLE_CHAT_AUTH_AUDIENCE: str = os.getenv(
        "GOOGLE_CHAT_AUTH_AUDIENCE",
        (
            f"{os.getenv('APP_URL', '').rstrip('/')}/users/bots/google-chat/events"
            if os.getenv("APP_URL")
            else ""
        ),
    )
    GOOGLE_CHAT_AUTH_LOGIN_URL: str = os.getenv(
        "GOOGLE_CHAT_AUTH_LOGIN_URL",
        f"{os.getenv('APP_URL', '').rstrip('/')}/bots/auth" if os.getenv("APP_URL") else "",
    )
    GOOGLE_CHAT_WEBHOOK_DEDUPE_TTL_SECONDS: int = 300
    GOOGLE_CHAT_API_BASE: str = "https://chat.googleapis.com/v1"
    GOOGLE_CHAT_HTTP_TIMEOUT_SECONDS: float = 10

    def get_app_credentials(self) -> Credentials:
        """Return ADC/WIF credentials scoped for Google Chat app-auth calls."""
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/chat.bot"])
        return credentials


slack_config = SlackConfig()
whatsapp_config = WhatsAppConfig()
google_chat_config = GoogleChatConfig()
sentry_config = SentryConfig()
otel_config = OTelConfig()


class Config:
    """Singleton wrapper around selected environment configuration map."""

    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # Only initialize once
        if not Config._initialized:
            self.env = os.getenv("ENV", "dev")
            self._config = CONFIG_MAP[self.env]
            logger.info(f"Config loaded for environment: {self.env}")
            Config._initialized = True

    @property
    def get_redis_client_details(self):
        """Return redis host, port, and password for selected environment config."""
        redis_details = self._config["redis_client"]
        return redis_details["host"], redis_details["port"], redis_details["password"]
