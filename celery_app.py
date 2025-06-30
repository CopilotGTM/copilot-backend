from celery import Celery
from core.configs.configs import Config

# Load configuration and Redis connection details
config = Config()
redis_host, redis_port, redis_password = config.get_redis_client_details
if redis_password:
    broker_url = f"rediss://:{redis_password}@{redis_host}:{redis_port}/0"
else:
    broker_url = f"rediss://{redis_host}:{redis_port}/0"
backend_url = broker_url

# Instantiate Celery, including the task module so it's registered
celery = Celery(
    "technical_deck_worker", broker=broker_url, backend=backend_url, include=["app_layer.tasks.technical_deck", "app_layer.tasks.slide_outline"]
)
# Explicit SSL config for Redis+SSL with Celery/Kombu
import ssl
SSL_CONFIG = {
    "ssl_cert_reqs": ssl.CERT_NONE
}
celery.conf.broker_use_ssl = SSL_CONFIG
celery.conf.redis_backend_use_ssl = SSL_CONFIG

# Auto-discover any other tasks in app_layer.tasks
celery.autodiscover_tasks(["app_layer.tasks"])
