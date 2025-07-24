import os
from typing import Optional

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


class AzureOpenAIConfig:
    """Configuration for Azure OpenAI services"""

    AZURE_OPENAI_API_KEY: Optional[str] = os.getenv("AZURE_OPENAI_API_KEY")
    AZURE_OPENAI_ENDPOINT: Optional[str] = os.getenv("AZURE_OPENAI_ENDPOINT")


class AzureEmbedderConfig:
    """Configuration for Azure Embedder service"""

    AZURE_EMBEDDER_OPENAI_API_KEY: Optional[str] = os.getenv("AZURE_EMBEDDER_OPENAI_API_KEY")
    AZURE_EMBEDDER_OPENAI_ENDPOINT: Optional[str] = os.getenv("AZURE_EMBEDDER_OPENAI_ENDPOINT")
    AZURE_EMBEDDER_DEPLOYMENT: Optional[str] = os.getenv("AZURE_EMBEDDER_DEPLOYMENT")


class CloudStorageConfig:
    """Configuration for Cloud Storage"""

    CLOUD_PROVIDER: str = os.getenv("CLOUD_PROVIDER", "aws")
    APPLICATION_STORAGE_NAME: Optional[str] = os.getenv("APPLICATION_STORAGE_NAME")
    APPLICATION_REGION: Optional[str] = os.getenv("APPLICATION_REGION")


class ResendConfig:
    """Configuration for Resend email service"""

    RESEND_API_KEY: Optional[str] = os.getenv("RESEND_API_KEY")
    FROM_EMAIL: Optional[str] = os.getenv("FROM_EMAIL")


class SuperAdminConfig:
    """Configuration for Super Admin"""

    SUPER_ADMIN_EMAIL: Optional[str] = os.getenv("SUPER_ADMIN_EMAIL")


# Create config instances
supabase_config = SupabaseConfig()
database_config = DatabaseConfig()
app_config = AppConfig()
azure_openai_config = AzureOpenAIConfig()
azure_embedder_config = AzureEmbedderConfig()
cloud_storage_config = CloudStorageConfig()
resend_config = ResendConfig()
super_admin_config = SuperAdminConfig()


class Config:
    def __init__(self):
        self.env = os.getenv("ENV", "dev")
        self._config = CONFIG_MAP[self.env]
        logger.info(f"Config loaded for environment: {self.env}")

    @property
    def get_redis_client_details(self):
        redis_details = self._config["redis_client"]
        return redis_details["host"], redis_details["port"], redis_details["password"]
