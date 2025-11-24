from celery.signals import worker_ready, worker_shutdown

from src.infra.messaging.redis_client import close_redis_pool
from src.workers.base.celery_base import create_celery_app
from src.workers.deal_worker.config import DealWorkerConfig

config = DealWorkerConfig()
app = create_celery_app("deal_worker", config, "src.workers.deal_worker.tasks")


@worker_ready.connect
def on_ready(sender=None, **kwargs):
    pass


@worker_shutdown.connect
def on_shutdown(sender=None, **kwargs):
    close_redis_pool()


if __name__ == "__main__":
    app.start()
