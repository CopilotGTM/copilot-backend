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
            # "stakeholder.trigger_analysis_all_orgs",  # Disabled — uses pipeline dispatch now
            # Calendar matching
            "deal.match_calendar_to_deals_org",
            "deal.match_calendar_to_deals",
            "deal.match_calendar_to_deals_all_orgs",
            # Email matching
            "deal.match_email_to_deals_org",
            "deal.match_email_to_deals",
            "deal.match_email_to_deals_all_orgs",
            # Meeting recording dispatch stays on the main deal queue because
            # these tasks are orchestration/I-O, not the long-running LLM jobs
            # that belong on `deal_jobs`.
            "deal.process_meeting_recording_webhook",
            "deal.send_meeting_recording",
            "deal.send_unsent_meeting_recordings_org",
            "deal.trigger_send_unsent_meeting_recordings_all_orgs",
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
            # Stale job cleanup tasks
            "deal.cleanup_stale_jobs_all_orgs",
            "deal.cleanup_stale_jobs_org",
            # Deal memory backfill
            "deal_memory.backfill_org",
            "deal_memory.trigger_backfill_all_orgs",
            # Deal asset post-upload processing
            "deal_asset.post_upload",
            # Stakeholder research retry (periodic scan for un-researched)
            "stakeholder_research.retry_pending_org",
            "stakeholder_research.trigger_retry_all_orgs",
        )

        deal_jobs_queue_tasks = (
            # Deal meeting job tasks (briefing, summary, follow-up)
            "deal_meeting_task.process",
            "meeting_summary_task.process",
            "meeting_prep_task.process",
            "deal_summary_task.process",
            # Stakeholder analysis tasks (LLM heavy)
            "stakeholder.analyze_deal_stakeholders_task",
            # "stakeholder.analyze_org_stakeholders_task",  # Disabled — uses pipeline dispatch now
            # Transcript upload tasks
            "transcript_upload_task.process",
            # Risk/Blocker extraction tasks
            "risk_blocker.extract_deal_risks_blockers_task",
            # Deal memory tasks
            "deal_memory.process_job",
            # Stakeholder research tasks
            "stakeholder_research.process_job",
        )

        self.task_routes = {
            **self._route_tasks(deal_queue_tasks, "deal"),
            **self._route_tasks(deal_jobs_queue_tasks, "deal_jobs"),
        }

        # Fixed deal schedules are owned by Kubernetes CronJobs.
        self.beat_schedule = {}

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
