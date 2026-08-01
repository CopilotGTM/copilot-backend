"""Lightweight Celery application for Kubernetes worker readiness probes.

The worker chart imports this module only from short-lived probe processes. It
can ping a running worker through Redis without importing task modules or
running tenant, database, Key Vault, and observability startup code.
"""

from celery import Celery

from src.workers.base.worker_config import BaseWorkerConfig

config = BaseWorkerConfig()
app = Celery(
    "worker_healthcheck",
    broker=config.broker_url,
    backend=config.backend_url,
)

if config.ssl_config:
    app.conf.broker_use_ssl = config.ssl_config
    app.conf.redis_backend_use_ssl = config.ssl_config

app.conf.update(
    broker_connection_retry=False,
    broker_connection_max_retries=0,
    broker_connection_timeout=2,
    broker_transport_options={
        "socket_timeout": 2,
        "socket_connect_timeout": 2,
        "socket_keepalive": True,
        "retry_on_timeout": False,
        "health_check_interval": 30,
    },
    worker_hijack_root_logger=False,
)
