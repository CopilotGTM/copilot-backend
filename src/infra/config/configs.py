import os
from typing import Optional
from dotenv import load_dotenv

# Load environment variables at the beginning of config module
load_dotenv(override=True)

from .constant import CONFIG_MAP
from ..utils.logger import get_logger

logger = get_logger()


class SupabaseConfig:
    """Configuration for Supabase authentication"""

    # Supabase project URL
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")

    # Supabase anon key (public)
    SUPABASE_ANON_KEY: str = os.getenv("SUPABASE_ANON_KEY", "")

    # Supabase service role key (private - for server-side operations)
    SUPABASE_SERVICE_ROLE_KEY: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

    # JWT secret for token verification
    SUPABASE_JWT_SECRET: str = os.getenv("SUPABASE_JWT_SECRET", "")

    # Enable/disable Supabase auth
    ENABLE_SUPABASE_AUTH: bool = (
        os.getenv("ENABLE_SUPABASE_AUTH", "true").lower() == "true"
    )


class DatabaseConfig:
    """Database configuration"""

    DATABASE_HOST: str = os.getenv("DATABASE_HOST", "localhost")
    DATABASE_PORT: str = os.getenv("DATABASE_PORT", "5432")
    DATABASE_USER: str = os.getenv("DATABASE_USER", "postgres")
    DATABASE_PASSWORD: str = os.getenv("DATABASE_PASSWORD", "")
    DATABASE_NAME: str = os.getenv("DATABASE_NAME", "postgres")
    DATABASE_SSL_MODE: str = os.getenv("DATABASE_SSL_MODE", "require")
    DATABASE_SSL_ROOT_CERT: Optional[str] = os.getenv("DATABASE_SSL_ROOT_CERT")
    POOL_RECYCLE_SEC: int = int(os.getenv("POOL_RECYCLE_SEC", "1800"))
    DATABASE_POOL_SIZE: int = int(os.getenv("DATABASE_POOL_SIZE", "5"))
    DATABASE_MAX_OVERFLOW: int = int(os.getenv("DATABASE_MAX_OVERFLOW", "2"))

    # guard against hangs when DB unavailable
    DATABASE_CONNECT_TIMEOUT_SEC: int = int(
        os.getenv("DATABASE_CONNECT_TIMEOUT_SEC", "10")
    )
    DATABASE_POOL_TIMEOUT_SEC: int = int(os.getenv("DATABASE_POOL_TIMEOUT_SEC", "30"))

    # default server-side statement timeout (ms). 0 disables.
    DATABASE_STATEMENT_TIMEOUT_MS: int = int(
        os.getenv("DATABASE_STATEMENT_TIMEOUT_MS", "60000")
    )


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
    APP_URL: str = os.getenv("APP_URL", "http://localhost:3000")

    # Security settings
    SECRET_KEY: str = os.getenv("SECRET_KEY", "your-secret-key-here")

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")


class OpenAIConfig:
    """Configuration for OpenAI services"""

    AZURE_OPENAI_API_KEY: Optional[str] = os.getenv("AZURE_OPENAI_API_KEY")
    AZURE_OPENAI_ENDPOINT: Optional[str] = os.getenv("AZURE_OPENAI_ENDPOINT")


class EmbedderConfig:
    """Configuration for Embedder service"""

    AZURE_EMBEDDER_OPENAI_API_KEY: Optional[str] = os.getenv(
        "AZURE_EMBEDDER_OPENAI_API_KEY"
    )
    AZURE_EMBEDDER_OPENAI_ENDPOINT: Optional[str] = os.getenv(
        "AZURE_EMBEDDER_OPENAI_ENDPOINT"
    )
    AZURE_EMBEDDER_DEPLOYMENT: Optional[str] = os.getenv("AZURE_EMBEDDER_DEPLOYMENT")


class CloudStorageConfig:
    """Configuration for Cloud Storage"""

    CLOUD_PROVIDER: str = os.getenv("CLOUD_PROVIDER", "aws")
    APPLICATION_STORAGE_NAME: Optional[str] = os.getenv("APPLICATION_STORAGE_NAME")
    ORGANISATION_STORAGE_NAME: Optional[str] = os.getenv("ORGANISATION_STORAGE_NAME")
    APPLICATION_REGION: Optional[str] = os.getenv("APPLICATION_REGION")


class ResendConfig:
    """Configuration for Resend email service"""

    RESEND_API_KEY: Optional[str] = os.getenv("RESEND_API_KEY")
    FROM_EMAIL: Optional[str] = os.getenv("FROM_EMAIL")


class SuperAdminConfig:
    """Configuration for Super Admin"""

    SUPER_ADMIN_EMAIL: Optional[str] = os.getenv("SUPER_ADMIN_EMAIL")


class SecretManagerConfig:
    """Configuration for Cloud Secret Manager"""

    AZURE_KEY_VAULT_URL: Optional[str] = os.getenv("AZURE_KEY_VAULT_URL")
    # New: bound individual secret fetch duration to avoid event-loop stalls
    SECRET_GET_TIMEOUT_SEC: int = int(os.getenv("SECRET_GET_TIMEOUT_SEC", "8"))


class CloudInfraConfig:
    """Configuration for Cloud Infrastructure"""

    AZURE_SUBSCRIPTION_ID: Optional[str] = os.getenv("AZURE_SUBSCRIPTION_ID")
    AZURE_RESOURCE_GROUP: Optional[str] = os.getenv("AZURE_RESOURCE_GROUP")
    # Optional auth parameters (may be absent when relying purely on workload identity)
    AZURE_TENANT_ID: Optional[str] = os.getenv("AZURE_TENANT_ID")
    AZURE_CLIENT_ID: Optional[str] = os.getenv("AZURE_CLIENT_ID")
    AZURE_CLIENT_SECRET: Optional[str] = os.getenv("AZURE_CLIENT_SECRET")
    AZURE_ADDITIONALLY_ALLOWED_TENANTS: Optional[str] = os.getenv(
        "AZURE_ADDITIONALLY_ALLOWED_TENANTS"
    )


class ChatFileUploadConfig:
    """Configuration for file uploads."""

    # Max file size in bytes (e.g., 50 * 1024 * 1024 for 50MB)
    MAX_FILE_SIZE_BYTES: int = int(os.getenv("MAX_UPLOAD_FILE_SIZE_BYTES", "52428800"))

    # Allowed MIME types for uploads
    ALLOWED_MIMETYPES: list[str] = [
        item.strip()
        for item in os.getenv(
            "ALLOWED_MIMETYPES",
            "application/pdf, text/plain, text/csv, application/msword, "
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document, "
            "application/vnd.ms-powerpoint, "
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ).split(",")
        if item.strip()
    ]


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

    ALLOWED_MIMETYPES: list[str] = [
        item.strip()
        for item in os.getenv(
            "KNOWLEDGE_ALLOWED_MIMETYPES",
            "application/pdf, text/plain",
        ).split(",")
        if item.strip()
    ]


class ThumbnailConfig:
    """Configuration for thumbnail generation behavior."""

    # If true, generate placeholder thumbnails for text files (text/plain)
    ENABLE_TEXT_PLACEHOLDERS: bool = (
        os.getenv("ENABLE_TEXT_PLACEHOLDERS", "true").lower() == "true"
    )

    # If true, when PDF renderer is unavailable or fails, do NOT fall back to placeholder automatically.
    # Keep default conservative (false) unless explicitly enabled.
    ENABLE_PDF_PLACEHOLDER_FALLBACK: bool = (
        os.getenv("ENABLE_PDF_PLACEHOLDER_FALLBACK", "false").lower() == "true"
    )


class PoolMonitorConfig:
    """Defaults for DB pool monitor and enforcement.

    These provide sensible base values so you don't need to set env vars.
    Env vars still override when present.
    """

    # Tick interval in seconds
    MONITOR_INTERVAL_SEC: float = float(os.getenv("DB_POOL_MONITOR_INTERVAL_SEC", "30"))

    # App vs Worker max checkout age (seconds)
    APP_MAX_CHECKOUT_SEC: int = int(
        os.getenv("APP_DB_CONN_MAX_CHECKOUT_SEC", "900")
    )  # 15 minutes
    WORKER_MAX_CHECKOUT_SEC: int = int(
        os.getenv("WORKER_DB_CONN_MAX_CHECKOUT_SEC", "1200")
    )  # 20 minutes

    # Force close behavior (recommended false; invalidate on check-in is safer)
    APP_FORCE_CLOSE: bool = (
        os.getenv("APP_DB_CONN_FORCE_CLOSE", "false").lower() == "true"
    )
    WORKER_FORCE_CLOSE: bool = (
        os.getenv("WORKER_DB_CONN_FORCE_CLOSE", "false").lower() == "true"
    )

    # Optional discovery/warmup defaults
    DISCOVER_MASTERS: bool = (
        os.getenv("DB_POOL_MONITOR_DISCOVER_MASTERS", "true").lower() == "true"
    )
    WARMUP_MIN_IDLE: int = int(os.getenv("DB_POOL_MONITOR_WARMUP_MIN_IDLE", "0"))


class RateLimitConfig:
    """Configuration for API rate limiting.

    Defaults are conservative for testing; override via env vars when needed.
    """

    PER_MINUTE: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "15"))
    PER_HOUR: int = int(os.getenv("RATE_LIMIT_PER_HOUR", "100"))

    # Lenient limits for read-only (GET) endpoints
    READ_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_READ_PER_MINUTE", "60"))
    READ_PER_HOUR: int = int(os.getenv("RATE_LIMIT_READ_PER_HOUR", "1000"))

    # Strict limits for authentication (login attempts)
    AUTH_LOGIN_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_AUTH_LOGIN_PER_MINUTE", "10"))
    AUTH_LOGIN_PER_HOUR: int = int(os.getenv("RATE_LIMIT_AUTH_LOGIN_PER_HOUR", "50"))


# Create config instances
supabase_config = SupabaseConfig()
database_config = DatabaseConfig()
app_config = AppConfig()
openai_config = OpenAIConfig()
embedder_config = EmbedderConfig()
cloud_storage_config = CloudStorageConfig()
resend_config = ResendConfig()
super_admin_config = SuperAdminConfig()
secret_manager_config = SecretManagerConfig()
cloud_infra_config = CloudInfraConfig()
chat_file_upload_config = ChatFileUploadConfig()
knowledge_file_upload_config = KnowledgeFileUploadConfig()
thumbnail_config = ThumbnailConfig()
pool_monitor_config = PoolMonitorConfig()
rate_limit_config = RateLimitConfig()


class Config:
    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Config, cls).__new__(cls)
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
        redis_details = self._config["redis_client"]
        return redis_details["host"], redis_details["port"], redis_details["password"]
