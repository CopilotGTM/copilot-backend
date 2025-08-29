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
    ENABLE_SUPABASE_AUTH: bool = os.getenv("ENABLE_SUPABASE_AUTH", "true").lower() == "true"


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
    DATABASE_POOL_SIZE: int = int(os.getenv("DATABASE_POOL_SIZE", "10"))
    DATABASE_MAX_OVERFLOW: int = int(os.getenv("DATABASE_MAX_OVERFLOW", "20"))
    DB_ADMIN_USER: Optional[str] = os.getenv("DB_ADMIN_USER")
    DB_ADMIN_PASSWORD_SECRET: Optional[str] = os.getenv("DB_ADMIN_PASSWORD_SECRET")


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

    AZURE_EMBEDDER_OPENAI_API_KEY: Optional[str] = os.getenv("AZURE_EMBEDDER_OPENAI_API_KEY")
    AZURE_EMBEDDER_OPENAI_ENDPOINT: Optional[str] = os.getenv("AZURE_EMBEDDER_OPENAI_ENDPOINT")
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


class CloudInfraConfig:
    """Configuration for Cloud Infrastructure"""

    AZURE_SUBSCRIPTION_ID: Optional[str] = os.getenv("AZURE_SUBSCRIPTION_ID")
    AZURE_RESOURCE_GROUP: Optional[str] = os.getenv("AZURE_RESOURCE_GROUP")
    # Optional auth parameters (may be absent when relying purely on workload identity)
    AZURE_TENANT_ID: Optional[str] = os.getenv("AZURE_TENANT_ID")
    AZURE_CLIENT_ID: Optional[str] = os.getenv("AZURE_CLIENT_ID")
    AZURE_CLIENT_SECRET: Optional[str] = os.getenv("AZURE_CLIENT_SECRET")
    AZURE_ADDITIONALLY_ALLOWED_TENANTS: Optional[str] = os.getenv("AZURE_ADDITIONALLY_ALLOWED_TENANTS")


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
        os.getenv("KNOWLEDGE_MAX_UPLOAD_FILE_SIZE_BYTES", os.getenv("MAX_UPLOAD_FILE_SIZE_BYTES", "104857600"))
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
    ENABLE_TEXT_PLACEHOLDERS: bool = os.getenv("ENABLE_TEXT_PLACEHOLDERS", "true").lower() == "true"

    # If true, when PDF renderer is unavailable or fails, do NOT fall back to placeholder automatically.
    # Keep default conservative (false) unless explicitly enabled.
    ENABLE_PDF_PLACEHOLDER_FALLBACK: bool = os.getenv("ENABLE_PDF_PLACEHOLDER_FALLBACK", "false").lower() == "true"


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


class Config:
    def __init__(self):
        self.env = os.getenv("ENV", "dev")
        self._config = CONFIG_MAP[self.env]
        logger.info(f"Config loaded for environment: {self.env}")

    @property
    def get_redis_client_details(self):
        redis_details = self._config["redis_client"]
        return redis_details["host"], redis_details["port"], redis_details["password"]
