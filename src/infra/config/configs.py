import os

from dotenv import load_dotenv

# Load environment variables at the beginning of config module
load_dotenv(override=True)

from ..utils.logger import get_logger
from .constant import CONFIG_MAP

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
    ENABLE_SUPABASE_AUTH: bool = os.getenv("ENABLE_SUPABASE_AUTH", "true").lower() == "true"


class DatabaseConfig:
    """Database configuration"""

    DATABASE_HOST: str = os.getenv("DATABASE_HOST", "localhost")
    DATABASE_PORT: str = os.getenv("DATABASE_PORT", "5432")
    DATABASE_USER: str = os.getenv("DATABASE_USER", "postgres")
    DATABASE_PASSWORD: str = os.getenv("DATABASE_PASSWORD", "")
    DATABASE_NAME: str = os.getenv("DATABASE_NAME", "postgres")
    DATABASE_SSL_MODE: str = os.getenv("DATABASE_SSL_MODE", "require")
    DATABASE_SSL_ROOT_CERT: str | None = os.getenv("DATABASE_SSL_ROOT_CERT")
    POOL_RECYCLE_SEC: int = int(os.getenv("POOL_RECYCLE_SEC", "1800"))
    DATABASE_POOL_SIZE: int = int(os.getenv("DATABASE_POOL_SIZE", "5"))
    DATABASE_MAX_OVERFLOW: int = int(os.getenv("DATABASE_MAX_OVERFLOW", "2"))

    # guard against hangs when DB unavailable
    DATABASE_CONNECT_TIMEOUT_SEC: int = int(os.getenv("DATABASE_CONNECT_TIMEOUT_SEC", "10"))
    DATABASE_POOL_TIMEOUT_SEC: int = int(os.getenv("DATABASE_POOL_TIMEOUT_SEC", "30"))

    # default server-side statement timeout (ms). 0 disables.
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
    SECRET_KEY: str = os.getenv("SECRET_KEY", "your-secret-key-here")

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")


class OpenAIConfig:
    """Configuration for OpenAI services"""

    AZURE_OPENAI_API_KEY: str | None = os.getenv("AZURE_OPENAI_API_KEY")
    AZURE_OPENAI_ENDPOINT: str | None = os.getenv("AZURE_OPENAI_ENDPOINT")


class EmbedderConfig:
    """Configuration for Embedder service"""

    AZURE_EMBEDDER_OPENAI_API_KEY: str | None = os.getenv("AZURE_EMBEDDER_OPENAI_API_KEY")
    AZURE_EMBEDDER_OPENAI_ENDPOINT: str | None = os.getenv("AZURE_EMBEDDER_OPENAI_ENDPOINT")
    AZURE_EMBEDDER_DEPLOYMENT: str | None = os.getenv("AZURE_EMBEDDER_DEPLOYMENT")


class CloudStorageConfig:
    """Configuration for Cloud Storage"""

    CLOUD_PROVIDER: str = os.getenv("CLOUD_PROVIDER", "aws")
    APPLICATION_STORAGE_NAME: str | None = os.getenv("APPLICATION_STORAGE_NAME")
    ORGANISATION_STORAGE_NAME: str | None = os.getenv("ORGANISATION_STORAGE_NAME")
    APPLICATION_REGION: str | None = os.getenv("APPLICATION_REGION")


class ResendConfig:
    """Configuration for Resend email service"""

    RESEND_API_KEY: str | None = os.getenv("RESEND_API_KEY")
    FROM_EMAIL: str | None = os.getenv("FROM_EMAIL")


class SuperAdminConfig:
    """Configuration for Super Admin"""

    SUPER_ADMIN_EMAIL: str | None = os.getenv("SUPER_ADMIN_EMAIL")


class ApolloConfig:
    """Configuration for Apollo.io B2B data enrichment.

    Apollo.io provides people and company enrichment, search,
    and sales intelligence capabilities.
    API Docs: https://docs.apollo.io/reference/introduction
    """

    APOLLO_API_KEY: str | None = os.getenv("APOLLO_API_KEY")
    APOLLO_BASE_URL: str = os.getenv("APOLLO_BASE_URL", "https://api.apollo.io/api/v1")

    # Rate limit tracking (credits per operation)
    # Get profile: 1 credit, Scrape profile: 2 credits
    # Email enrichment: 4 credits (if found), Phone: 10 credits (if found)


class LinkedInEnrichmentConfig:
    """Configuration for LinkedIn profile enrichment via Clado API."""

    LINKEDIN_CLADO_API_KEY: str | None = os.getenv("LINKEDIN_CLADO_API_KEY")
    CLADO_BASE_URL: str = os.getenv("CLADO_BASE_URL", "https://api.clado.ai")


class TeamsConfig:
    """Configuration for Microsoft Teams Bot integration.

    Bot credentials are stored in Azure Key Vault (master vault) and fetched at runtime.
    Key Vault secret names:
    - teams-bot-app-id: Azure Bot App ID
    - teams-bot-app-secret: Azure Bot App Secret (client secret)
    - teams-bot-tenant-id: Azure AD Tenant ID (for single-tenant bot)

    Environment variables:
    - TEAMS_ENABLED: Enable/disable Teams bot functionality
    - TEAMS_AUTH_LOGIN_URL: URL to your login page for Teams auth flow
    - TEAMS_AUTH_CALLBACK_URL: Callback URL after login
    """

    # Feature flag - can be overridden via env var
    TEAMS_ENABLED: bool = os.getenv("TEAMS_ENABLED", "true").lower() == "true"

    # Key Vault secret names (these are the names stored in master Key Vault)
    SECRET_NAME_BOT_APP_ID: str = "teams-bot-app-id"
    SECRET_NAME_BOT_APP_SECRET: str = "teams-bot-app-secret"
    SECRET_NAME_BOT_TENANT_ID: str = "teams-bot-tenant-id"

    # Optional direct credentials (env override; use with caution)
    TEAMS_BOT_APP_ID: str | None = os.getenv("TEAMS_BOT_APP_ID")
    TEAMS_BOT_APP_SECRET: str | None = os.getenv("TEAMS_BOT_APP_SECRET")
    TEAMS_BOT_TENANT_ID: str | None = os.getenv("TEAMS_BOT_TENANT_ID")

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


class NangoIntegrationConfig:
    """Configuration for Nango OAuth integration management

    Note: As of July 31, 2025, Nango deprecated public keys in favor of Connect Sessions.
    The new flow uses session tokens for authorization.
    """

    NANGO_SECRET_KEY: str = os.getenv("NANGO_SECRET_KEY", "")
    NANGO_BASE_URL: str = os.getenv("NANGO_BASE_URL", "https://api.nango.dev")

    # Integration behavior settings
    INTEGRATION_TOKEN_REFRESH_BUFFER_MINUTES: int = int(
        os.getenv("INTEGRATION_TOKEN_REFRESH_BUFFER_MINUTES", "30")
    )
    INTEGRATION_SYNC_RETRY_ATTEMPTS: int = int(os.getenv("INTEGRATION_SYNC_RETRY_ATTEMPTS", "3"))
    INTEGRATION_MAX_CONCURRENT_SYNCS: int = int(os.getenv("INTEGRATION_MAX_CONCURRENT_SYNCS", "5"))

    # OAuth callback URL base (for frontend redirects after connection)
    OAUTH_CALLBACK_BASE_URL: str | None = os.getenv("APP_URL")


class SecretManagerConfig:
    """Configuration for Cloud Secret Manager"""

    # Primary Key Vault for org-specific secrets (DB passwords per tenant)
    AZURE_KEY_VAULT_URL: str | None = os.getenv("AZURE_KEY_VAULT_URL")

    # Master Key Vault for global/shared secrets (Teams bot, API keys, etc.)
    # Falls back to AZURE_KEY_VAULT_URL if not set
    AZURE_MASTER_KEY_VAULT_URL: str | None = os.getenv(
        "AZURE_MASTER_KEY_VAULT_URL", os.getenv("AZURE_KEY_VAULT_URL")
    )

    # New: bound individual secret fetch duration to avoid event-loop stalls
    SECRET_GET_TIMEOUT_SEC: int = int(os.getenv("SECRET_GET_TIMEOUT_SEC", "8"))


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

    # Allowed MIME types for uploads
    # Requirement: restrict to only PDF and plain text for chat attachments.
    ALLOWED_MIMETYPES: list[str] = [
        item.strip()
        for item in os.getenv(
            "ALLOWED_MIMETYPES",
            "application/pdf, text/plain, text/csv, text/tab-separated-values, text/tsv, "
            "text/markdown, text/x-markdown, application/x-ndjson, application/ndjson, application/jsonl, application/json",
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
    # Backward-compatible prefix list (kept narrow). If you want to allow every image/* type
    # set THUMBNAIL_ENABLE_IMAGE_PREFIX_FALLBACK=true — otherwise we only accept explicit list.
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
    def all_supported_mimetypes(cls) -> set[str]:  # convenient aggregated accessor
        return (
            set(cls.PDF_MIMETYPES)
            | set(cls.TEXT_LABEL_MAP.keys())
            | set(cls.DOCUMENT_LABEL_MAP.keys())
        )

    @classmethod
    def is_supported(cls, mimetype: str) -> bool:
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


class PoolMonitorConfig:
    """Defaults for DB pool monitor and enforcement.

    These provide sensible base values so you don't need to set env vars.
    Env vars still override when present.
    """

    # Tick interval in seconds (default: 300s = 5 minutes)
    MONITOR_INTERVAL_SEC: float = float(os.getenv("DB_POOL_MONITOR_INTERVAL_SEC", "300"))

    # App vs Worker max checkout age (seconds)
    APP_MAX_CHECKOUT_SEC: int = int(os.getenv("APP_DB_CONN_MAX_CHECKOUT_SEC", "900"))  # 15 minutes
    WORKER_MAX_CHECKOUT_SEC: int = int(
        os.getenv("WORKER_DB_CONN_MAX_CHECKOUT_SEC", "1200")
    )  # 20 minutes

    # Force close behavior (recommended false; invalidate on check-in is safer)
    APP_FORCE_CLOSE: bool = os.getenv("APP_DB_CONN_FORCE_CLOSE", "false").lower() == "true"
    WORKER_FORCE_CLOSE: bool = os.getenv("WORKER_DB_CONN_FORCE_CLOSE", "false").lower() == "true"

    # Optional discovery/warmup defaults
    DISCOVER_MASTERS: bool = os.getenv("DB_POOL_MONITOR_DISCOVER_MASTERS", "true").lower() == "true"
    WARMUP_MIN_IDLE: int = int(os.getenv("DB_POOL_MONITOR_WARMUP_MIN_IDLE", "0"))


class RateLimitConfig:
    """Configuration for API rate limiting.

    Defaults are conservative for testing; override via env vars when needed.
    """

    PER_MINUTE: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "120"))
    PER_HOUR: int = int(os.getenv("RATE_LIMIT_PER_HOUR", "2000"))

    # Lenient limits for read-only (GET) endpoints
    READ_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_READ_PER_MINUTE", "120"))
    READ_PER_HOUR: int = int(os.getenv("RATE_LIMIT_READ_PER_HOUR", "2000"))

    # Strict limits for authentication (login attempts) - per IP+email
    AUTH_LOGIN_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_AUTH_LOGIN_PER_MINUTE", "50"))
    AUTH_LOGIN_PER_HOUR: int = int(os.getenv("RATE_LIMIT_AUTH_LOGIN_PER_HOUR", "100"))

    # IP-only total limit for login (prevents credential stuffing across many emails)
    LOGIN_IP_TOTAL_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_LOGIN_IP_TOTAL_PER_MINUTE", "100"))
    LOGIN_IP_TOTAL_PER_HOUR: int = int(os.getenv("RATE_LIMIT_LOGIN_IP_TOTAL_PER_HOUR", "300"))


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
# NOTE: thumbnail_config now sourced from thumbnail_config.py for modularity.
pool_monitor_config = PoolMonitorConfig()
rate_limit_config = RateLimitConfig()
nango_integration_config = NangoIntegrationConfig()
apollo_config = ApolloConfig()
linkedin_enrichment_config = LinkedInEnrichmentConfig()
teams_config = TeamsConfig()


class Config:
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
        redis_details = self._config["redis_client"]
        return redis_details["host"], redis_details["port"], redis_details["password"]
