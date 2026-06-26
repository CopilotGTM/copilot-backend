"""Deal insight tasks module.

Tasks for deal-level AI/agent workflows:
- Deal summary: Comprehensive summary analyzing all meetings for a deal
- Stakeholder analysis: AI-powered analysis of stakeholder stance, decision power, etc.
- Risk/blocker extraction: AI extraction of risks and blockers from deal communications
"""

from src.workers.deal_worker.tasks.deal_insight_tasks.contact_research_tasks import (
    process_contact_research_job,
)
from src.workers.deal_worker.tasks.deal_insight_tasks.deal_memory_tasks import (
    build_deal_memory_org,
    process_deal_memory_job,
    trigger_deal_memory_backfill_all_orgs,
)
from src.workers.deal_worker.tasks.deal_insight_tasks.deal_summary_task import (
    process_deal_summary_job,
)
from src.workers.deal_worker.tasks.deal_insight_tasks.deal_task_sync_task import (
    sync_deal_tasks_from_summary,
)
from src.workers.deal_worker.tasks.deal_insight_tasks.deal_update_task import (
    process_deal_update_job,
)
from src.workers.deal_worker.tasks.deal_insight_tasks.risk_blocker_extraction_task import (
    extract_deal_risks_blockers_task,
    trigger_extraction_all_orgs,
)
from src.workers.deal_worker.tasks.deal_insight_tasks.stakeholder_analysis_task import (
    analyze_deal_stakeholders_task,
)
from src.workers.deal_worker.tasks.deal_insight_tasks.stakeholder_research_tasks import (
    process_stakeholder_research_job,
    retry_pending_research_org,
    trigger_retry_pending_research_all_orgs,
)

__all__ = [
    # Deal summary
    "process_deal_summary_job",
    # Deal update
    "process_deal_update_job",
    # Stakeholder analysis
    "analyze_deal_stakeholders_task",
    # Stakeholder research
    "process_stakeholder_research_job",
    "retry_pending_research_org",
    "trigger_retry_pending_research_all_orgs",
    # Contact research
    "process_contact_research_job",
    # Risk/blocker extraction
    "extract_deal_risks_blockers_task",
    "trigger_extraction_all_orgs",
    # Deal memory
    "process_deal_memory_job",
    "build_deal_memory_org",
    "trigger_deal_memory_backfill_all_orgs",
    # Deal task sync
    "sync_deal_tasks_from_summary",
]
