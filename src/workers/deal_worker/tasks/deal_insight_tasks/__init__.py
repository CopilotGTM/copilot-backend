"""Deal insight tasks module.

Tasks for deal-level AI/agent workflows:
- Deal summary: Comprehensive summary analyzing all meetings for a deal
"""

from src.workers.deal_worker.tasks.deal_insight_tasks.deal_summary_task import (
    process_deal_summary_job,
)

__all__ = ["process_deal_summary_job"]
