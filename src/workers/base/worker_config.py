import os
import ssl

from dotenv import load_dotenv

from src.infra.config.configs import Config


class BaseWorkerConfig:
    """Base configuration for all workers"""

    def __init__(self):
        # Load environment variables from .env file
        load_dotenv(override=True)

        self.config = Config()
        self.redis_host, self.redis_port, self.redis_password = self.config.get_redis_client_details
        self.broker_url = self._get_broker_url()
        self.backend_url = self.broker_url
        self.ssl_config = self._get_ssl_config()

    def _get_broker_url(self):
        # Use redis (non-SSL) for localhost or local Docker containers
        ssl_enabled = not (
            self.redis_host in ["localhost", "127.0.0.1", "redis"]
            or self.redis_host.startswith("192.168.")
            or self.config.env == "local"
        )
        redis_scheme = "redis" if not ssl_enabled else "rediss"

        redis_password_part = f":{self.redis_password}@" if self.redis_password else ""
        broker_url = f"{redis_scheme}://{redis_password_part}{self.redis_host}:{self.redis_port}/0"
        return broker_url

    def _get_ssl_config(self):
        # Only use SSL for remote Redis, not localhost or local Docker containers
        ssl_enabled = not (
            self.redis_host in ["localhost", "127.0.0.1", "redis"]
            or self.redis_host.startswith("192.168.")
            or self.config.env == "local"
        )
        if ssl_enabled:
            # Default to CERT_REQUIRED for security (instead of CERT_NONE)
            ssl_cert_reqs_str = os.environ.get("REDIS_SSL_CERT_REQS", "CERT_REQUIRED")
            ssl_cert_reqs_map = {
                "CERT_NONE": ssl.CERT_NONE,
                "CERT_OPTIONAL": ssl.CERT_OPTIONAL,
                "CERT_REQUIRED": ssl.CERT_REQUIRED,
            }
            ssl_cert_reqs = ssl_cert_reqs_map.get(ssl_cert_reqs_str, ssl.CERT_REQUIRED)

            # Return secure SSL configuration
            return {
                "ssl_cert_reqs": ssl_cert_reqs,
                "ssl_check_hostname": True,  # Enable hostname verification
            }
        return None
