from celery import Celery


def create_celery_app(worker_name, config, tasks_module):
    """Factory function to create a Celery app for a specific worker."""

    celery_app = Celery(
        worker_name,
        broker=config.broker_url,
        backend=config.backend_url,
        include=[tasks_module],
    )

    # Apply SSL options if they are present in the config
    if config.ssl_config:
        celery_app.conf.broker_use_ssl = config.ssl_config
        celery_app.conf.redis_backend_use_ssl = config.ssl_config

    # Set worker-specific queue configurations
    if hasattr(config, "task_queues"):
        celery_app.conf.task_queues = config.task_queues

    if hasattr(config, "task_default_queue"):
        celery_app.conf.task_default_queue = config.task_default_queue

    # Set default exchange settings
    if hasattr(config, "task_default_exchange"):
        celery_app.conf.task_default_exchange = config.task_default_exchange

    if hasattr(config, "task_default_exchange_type"):
        celery_app.conf.task_default_exchange_type = config.task_default_exchange_type

    if hasattr(config, "task_default_routing_key"):
        celery_app.conf.task_default_routing_key = config.task_default_routing_key

    # Set task routing if defined in the worker's config
    if hasattr(config, "task_routes"):
        celery_app.conf.task_routes = config.task_routes

    # Set beat schedule if defined in the worker's config (for periodic tasks)
    if hasattr(config, "beat_schedule") and config.beat_schedule:
        celery_app.conf.beat_schedule = config.beat_schedule

    # Default time limits (can be overridden by worker-specific config)
    default_time_limit = 300  # 5 minutes
    default_soft_time_limit = 240  # 4 minutes

    # Allow worker-specific time limits
    if hasattr(config, "task_time_limit"):
        default_time_limit = config.task_time_limit
    if hasattr(config, "task_soft_time_limit"):
        default_soft_time_limit = config.task_soft_time_limit

    # Add essential Celery configuration for task consumption
    celery_app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        worker_prefetch_multiplier=1,  # Important for fair task distribution
        task_acks_late=True,  # Prevent task loss
        worker_disable_rate_limits=True,  # Disable rate limiting for better performance
        # Connection settings
        broker_connection_retry_on_startup=True,
        broker_connection_retry=True,
        broker_connection_max_retries=10,
        # Task execution settings
        task_track_started=True,
        task_time_limit=default_time_limit,
        task_soft_time_limit=default_soft_time_limit,
        # Worker settings
        worker_hijack_root_logger=False,
        worker_log_color=False,
    )

    return celery_app
