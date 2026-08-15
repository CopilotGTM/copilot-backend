"""Celery queues, routes, limits, and recovery schedules for chat work."""

from kombu import Exchange, Queue

from src.workers.base.worker_config import BaseWorkerConfig


class ChatWorkerConfig(BaseWorkerConfig):
    """Configuration for the Chat Worker."""

    def __init__(self):
        super().__init__()

        # Chat worker needs longer time limits for agent responses with multiple tool calls.
        # Voice sideband sessions use task-level overrides because live calls run longer.
        self.task_time_limit = 600  # 10 minutes hard limit
        self.task_soft_time_limit = 540  # 9 minutes soft limit

        # Fix: declare a dedicated direct exchange and bind each queue with a routing
        # key matching its queue name. Also add routing_key to task_routes so Celery
        # produces messages with the correct routing key.

        chat_exchange = Exchange("chat", type="direct")

        self.task_default_exchange = "chat"
        self.task_default_exchange_type = "direct"
        self.task_default_queue = "attachments"
        self.task_default_routing_key = "attachments"

        self.task_queues = (
            Queue("attachments", exchange=chat_exchange, routing_key="attachments"),
            Queue("chat_processing", exchange=chat_exchange, routing_key="chat_processing"),
            Queue("chat_messages", exchange=chat_exchange, routing_key="chat_messages"),
            Queue("voice_realtime", exchange=chat_exchange, routing_key="voice_realtime"),
            Queue("chat_maintenance", exchange=chat_exchange, routing_key="chat_maintenance"),
        )

        self.task_routes = {
            "chat.activate_automation_event": {
                "queue": "chat_processing",
                "routing_key": "chat_processing",
            },
            "chat.recover_automation_events_all_orgs": {
                "queue": "chat_maintenance",
                "routing_key": "chat_maintenance",
            },
            "chat.recover_automation_events_org": {
                "queue": "chat_maintenance",
                "routing_key": "chat_maintenance",
            },
            "chat.dispatch_due_automation_schedules_all_orgs": {
                "queue": "chat_maintenance",
                "routing_key": "chat_maintenance",
            },
            "chat.dispatch_due_automation_schedules_org": {
                "queue": "chat_maintenance",
                "routing_key": "chat_maintenance",
            },
            "chat.execute_agent_run": {
                "queue": "chat_messages",
                "routing_key": "chat_messages",
            },
            "chat.recover_agent_runs": {
                "queue": "chat_maintenance",
                "routing_key": "chat_maintenance",
            },
            # Delivery has its own queue so a slow provider cannot block model
            # execution messages; its periodic repair probe stays on maintenance.
            "chat.deliver_agent_run": {
                "queue": "chat_messages",
                "routing_key": "chat_messages",
            },
            "chat.recover_agent_run_deliveries": {
                "queue": "chat_maintenance",
                "routing_key": "chat_maintenance",
            },
            "attachments.post_upload": {
                "queue": "attachments",
                "routing_key": "attachments",
            },
            "chat.test_task": {
                "queue": "chat_processing",
                "routing_key": "chat_processing",
            },
            "chat.ping": {"queue": "chat_processing", "routing_key": "chat_processing"},
            "chat.process_message": {
                "queue": "chat_messages",
                "routing_key": "chat_messages",
            },
            "chat.process_bot_message": {
                "queue": "chat_messages",
                "routing_key": "chat_messages",
            },
            "chat.voice_realtime_sideband": {
                "queue": "voice_realtime",
                "routing_key": "voice_realtime",
            },
            # Reminder scheduler tasks
            "chat.poll_due_reminders": {
                "queue": "chat_maintenance",
                "routing_key": "chat_maintenance",
            },
            "chat.recovery_sweep_reminders": {
                "queue": "chat_maintenance",
                "routing_key": "chat_maintenance",
            },
            "chat.execute_reminder": {
                "queue": "chat_messages",
                "routing_key": "chat_messages",
            },
            # Stale job cleanup tasks
            "chat.cleanup_stale_jobs_all_orgs": {
                "queue": "chat_maintenance",
                "routing_key": "chat_maintenance",
            },
        }

        # Each sweep repeats from durable state, so an older queued tick is redundant.
        # Expire it before the next tick to prevent outage recovery from replaying fan-outs.
        self.beat_schedule = {
            "recover-automation-events": {
                "task": "chat.recover_automation_events_all_orgs",
                "schedule": 60.0,
                "options": {
                    "queue": "chat_maintenance",
                    "routing_key": "chat_maintenance",
                    "expires": 55,
                },
            },
            "dispatch-due-automation-schedules": {
                "task": "chat.dispatch_due_automation_schedules_all_orgs",
                "schedule": 60.0,
                "options": {
                    "queue": "chat_maintenance",
                    "routing_key": "chat_maintenance",
                    "expires": 55,
                },
            },
            "recover-agent-runs": {
                "task": "chat.recover_agent_runs",
                "schedule": 30.0,
                "options": {
                    "queue": "chat_maintenance",
                    "routing_key": "chat_maintenance",
                    "expires": 25,
                },
            },
            # The sweep republishes durable rows whose broker publication or
            # worker lease was lost; it does not rerun the completed agent.
            "recover-agent-run-deliveries": {
                "task": "chat.recover_agent_run_deliveries",
                "schedule": 30.0,
                "options": {
                    "queue": "chat_maintenance",
                    "routing_key": "chat_maintenance",
                    "expires": 25,
                },
            },
            "poll-due-reminders": {
                "task": "chat.poll_due_reminders",
                "schedule": 60.0,
                "options": {
                    "queue": "chat_maintenance",
                    "routing_key": "chat_maintenance",
                    "expires": 55,
                },
            },
            "cleanup-stale-chat-jobs": {
                "task": "chat.cleanup_stale_jobs_all_orgs",
                "schedule": 300.0,
                "options": {
                    "queue": "chat_maintenance",
                    "routing_key": "chat_maintenance",
                    "expires": 295,
                },
            },
        }
