from celery import Celery
from src.infra.config.configs import Config
from dotenv import load_dotenv
import os
import ssl

# Load .env variables
load_dotenv()

# Load configuration and Redis connection details
config = Config()
redis_host, redis_port, redis_password = config.get_redis_client_details
# Use SSL (rediss://) for non-local envs, plain redis for local
redis_scheme = "redis" if config.env == "local" else "rediss"
redis_password_part = f":{redis_password}@" if redis_password else ""
broker_url = f"{redis_scheme}://{redis_password_part}{redis_host}:{redis_port}/0"
backend_url = broker_url

# Set SSL options for non-local environments
broker_use_ssl = None
backend_use_ssl = None
if config.env != "local":
    # Map string to ssl constant
    ssl_cert_reqs_str = os.environ.get("REDIS_SSL_CERT_REQS", "CERT_NONE")
    ssl_cert_reqs_map = {
        "CERT_NONE": ssl.CERT_NONE,
        "CERT_OPTIONAL": ssl.CERT_OPTIONAL,
        "CERT_REQUIRED": ssl.CERT_REQUIRED,
    }
    ssl_cert_reqs = ssl_cert_reqs_map.get(ssl_cert_reqs_str, ssl.CERT_NONE)
    broker_use_ssl = {"ssl_cert_reqs": ssl_cert_reqs}
    backend_use_ssl = {"ssl_cert_reqs": ssl_cert_reqs}

# Instantiate Celery, including the task modules so they're registered
celery = Celery(
    "technical_deck_worker",
    broker=broker_url,
    backend=backend_url,
    include=["src.tasks.technical_deck", "src.tasks.slide_outline"],
)

# Apply SSL options if set
if broker_use_ssl:
    celery.conf.broker_use_ssl = broker_use_ssl
if backend_use_ssl:
    celery.conf.redis_backend_use_ssl = backend_use_ssl

# Auto-discover any other tasks in src.tasks
celery.autodiscover_tasks(["src.tasks"])
