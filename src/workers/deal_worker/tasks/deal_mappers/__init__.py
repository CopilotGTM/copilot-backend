"""Deal communication hydration and stakeholder task registrations.

This module contains the task architecture for deal mapping:

Live email/calendar sync and stakeholder changes converge on
``deal.hydrate_contact_communications``. The older split mapper behavior is
consolidated behind this worker entrypoint.
"""

from src.workers.deal_worker.tasks.deal_mappers.contact_communication_hydration_tasks import (
    hydrate_contact_communications_task,
)
from src.workers.deal_worker.tasks.deal_mappers.stakeholder_tasks import (
    sync_all_deals_stakeholders_org_task,
    sync_deal_stakeholders_task,
)

__all__ = [
    # Task 1: Stakeholder Sync
    "sync_deal_stakeholders_task",
    "sync_all_deals_stakeholders_org_task",
    "hydrate_contact_communications_task",
]
