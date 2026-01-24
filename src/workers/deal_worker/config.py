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

        # Route tasks to queues in a compact, readable way
        deal_queue_tasks = (
            # Stakeholder sync
            "stakeholder.sync_deal_stakeholders",
            "stakeholder.sync_all_deals_stakeholders_org",
            "stakeholder.sync_all_deals_stakeholders_all_orgs",
            "stakeholder.trigger_analysis_all_orgs",
            # Deal import
            "deal_import_task.process",
            # Calendar matching
            "deal.match_calendar_to_deals_org",
            "deal.match_calendar_to_deals",
            "deal.match_calendar_to_deals_all_orgs",
            # Email matching
            "deal.match_email_to_deals_org",
            "deal.match_email_to_deals",
            "deal.match_email_to_deals_all_orgs",
            # Timeline event tasks
            "deal.sync_deal_timeline",
            "deal.sync_org_timeline",
            "deal.exclude_timeline_event",
            "deal.trigger_timeline_sync_all_orgs",
            # Risk/Blocker extraction trigger
            "risk_blocker.trigger_extraction_all_orgs",
            # Transcript fetch tasks
            "deal.trigger_transcript_fetch_all_orgs",
            "deal.fetch_meeting_transcripts_org",
        )

        deal_jobs_queue_tasks = (
            # Deal meeting job tasks (briefing, summary, follow-up)
            "deal_meeting_task.process",
            "meeting_summary_task.process",
            "meeting_prep_task.process",
            "deal_summary_task.process",
            # Stakeholder analysis tasks (LLM heavy)
            "stakeholder.analyze_deal_stakeholders_task",
            "stakeholder.analyze_org_stakeholders_task",
            # Risk/Blocker extraction tasks
            "risk_blocker.extract_deal_risks_blockers_task",
        )

        self.task_routes = {
            **self._route_tasks(deal_queue_tasks, "deal"),
            **self._route_tasks(deal_jobs_queue_tasks, "deal_jobs"),
        }

        # Celery Beat schedule for periodic tasks
        self.beat_schedule = {
            # HOURLY: Stakeholder sync → triggers calendar + email matching
            # This runs the full discovery cycle every hour
            "hourly-deal-mapping": {
                "task": "stakeholder.sync_all_deals_stakeholders_all_orgs",
                "schedule": crontab(minute=0),  # Every hour at :00
                "options": {"queue": "deal", "routing_key": "deal"},
            },
            # # HOURLY: Timeline sync - safety net to fill gaps from missed inline creation
            # # Runs at :30 to avoid overlap with deal-mapping at :00
            "timeline-sync": {
                "task": "deal.trigger_timeline_sync_all_orgs",
                "schedule": crontab(minute=30),  # Every 30 minutes
                "options": {"queue": "deal", "routing_key": "deal"},
            },
            # # NIGHTLY: Heavy LLM tasks (2 AM UTC)
            "nightly-stakeholder-analysis": {
                "task": "stakeholder.trigger_analysis_all_orgs",
                "schedule": crontab(hour=2, minute=0),
                "options": {"queue": "deal", "routing_key": "deal"},
            },
            # HALF-HOURLY: Transcript fetch and meeting summary pipeline
            # Runs at :15 and :45 to avoid overlap with other scheduled tasks
            # After fetching transcripts, automatically triggers:
            # - Meeting summaries (sequential, oldest first)
            # - Deal summaries (after all meeting summaries complete)
            "half-hourly-transcript-fetch-pipeline": {
                "task": "deal.trigger_transcript_fetch_all_orgs",
                "schedule": crontab(minute="5,35"),  # Every 30 minutes
                "options": {"queue": "deal", "routing_key": "deal"},
            },
        }

    @staticmethod
    def _route_tasks(tasks, queue):
        """Return a routing dict for the provided tasks and queue."""
        return {
            task: {
                "queue": queue,
                "routing_key": queue,
            }
            for task in tasks
        }
