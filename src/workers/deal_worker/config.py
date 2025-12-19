from celery.schedules import crontab
from kombu import Exchange, Queue

from src.workers.base.worker_config import BaseWorkerConfig


class DealWorkerConfig(BaseWorkerConfig):
    """Configuration for the Deal Worker.
    Handles deal-related background processing:
    - Deal-meeting matching and association
    - Meeting briefing and summary generation
    - Stakeholder synchronization
    """

    def __init__(self):
        super().__init__()

        # Time limits (default 5 minutes, can be adjusted for long-running tasks)
        self.task_time_limit = 300  # 5 minutes hard limit
        self.task_soft_time_limit = 240  # 4 minutes soft limit

        # Define exchange (use direct type for explicit routing)
        deal_exchange = Exchange("deal", type="direct")

        # Exchange defaults
        self.task_default_exchange = "deal"
        self.task_default_exchange_type = "direct"
        self.task_default_queue = "deal"
        self.task_default_routing_key = "deal"

        # Define queues
        self.task_queues = (
            Queue(
                "deal",
                exchange=deal_exchange,
                routing_key="deal",
            ),
            Queue(
                "deal_jobs",
                exchange=deal_exchange,
                routing_key="deal_jobs",
            ),
        )

        # Route tasks to queues
        self.task_routes = {
            # Deal matching and processing tasks -> deal queue
            "deal.process_deal_meetings_task": {
                "queue": "deal",
                "routing_key": "deal",
            },
            "deal.trigger_deal_meeting_processing_all_orgs": {
                "queue": "deal",
                "routing_key": "deal",
            },
            "deal.process_single_deal_task": {
                "queue": "deal",
                "routing_key": "deal",
            },
            # Deal email matching tasks -> deal queue
            "deal.process_deal_emails_task": {
                "queue": "deal",
                "routing_key": "deal",
            },
            "deal.trigger_deal_email_processing_all_orgs": {
                "queue": "deal",
                "routing_key": "deal",
            },
            # Deal meeting job tasks (briefing, summary, followup) -> deal_jobs queue
            "deal_meeting_task.process": {
                "queue": "deal_jobs",
                "routing_key": "deal_jobs",
            },
            # Stakeholder sync tasks -> deal queue
            "stakeholder.sync_from_meeting_task": {
                "queue": "deal",
                "routing_key": "deal",
            },
            # Stakeholder analysis tasks -> deal_jobs queue (uses LLM)
            "stakeholder.analyze_deal_stakeholders_task": {
                "queue": "deal_jobs",
                "routing_key": "deal_jobs",
            },
            "stakeholder.analyze_org_stakeholders_task": {
                "queue": "deal_jobs",
                "routing_key": "deal_jobs",
            },
            "stakeholder.trigger_analysis_all_orgs": {
                "queue": "deal",
                "routing_key": "deal",
            },
            "deal.trigger_timeline_sync_all_orgs": {
                "queue": "deal",
            },
            # Timeline event tasks -> deal queue
            "deal.sync_deal_timeline": {
                "queue": "deal",
                "routing_key": "deal",
            },
            "deal.sync_org_timeline": {
                "queue": "deal",
                "routing_key": "deal",
            },
            "deal.exclude_timeline_event": {
                "queue": "deal",
                "routing_key": "deal",
            },
        }

        # Celery Beat schedule for periodic tasks
        self.beat_schedule = {
            "process-deal-meetings-all-orgs": {
                "task": "deal.trigger_deal_meeting_processing_all_orgs",
                "schedule": crontab(minute=0),  # Run every hour at :00
                "options": {
                    "queue": "deal",
                    "routing_key": "deal",
                },
            },
            "analyze-stakeholders-all-orgs": {
                "task": "stakeholder.trigger_analysis_all_orgs",
                "schedule": crontab(minute=0),  # Run every hour at :00
                "options": {
                    "queue": "deal",
                    "routing_key": "deal",
                },
            },
            "process-deal-emails-all-orgs": {
                "task": "deal.trigger_deal_email_processing_all_orgs",
                "schedule": crontab(minute=0),  # Run every hour at :00
                "options": {
                    "queue": "deal",
                    "routing_key": "deal",
                },
            },
        }
