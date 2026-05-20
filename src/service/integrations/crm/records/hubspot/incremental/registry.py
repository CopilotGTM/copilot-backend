"""Registry for HubSpot incremental object refreshers."""

from __future__ import annotations

from src.service.integrations.crm.records.hubspot.incremental.company_incremental_refresher import (
    HubSpotCompanyIncrementalRefresher,
)
from src.service.integrations.crm.records.hubspot.incremental.contact_incremental_refresher import (
    HubSpotContactIncrementalRefresher,
)
from src.service.integrations.crm.records.hubspot.incremental.deal_incremental_refresher import (
    HubSpotDealIncrementalRefresher,
)
from src.service.integrations.crm.records.hubspot.incremental.note_incremental_refresher import (
    HubSpotNoteIncrementalRefresher,
)
from src.service.integrations.crm.records.hubspot.incremental.object_refresher import (
    HubSpotIncrementalObjectRefresher,
)
from src.service.integrations.crm.records.hubspot.incremental.task_incremental_refresher import (
    HubSpotTaskIncrementalRefresher,
)

HUBSPOT_INCREMENTAL_OBJECT_KEYS = ("deals", "companies", "contacts", "tasks", "notes")


def build_hubspot_incremental_refresher_registry() -> dict[
    str,
    HubSpotIncrementalObjectRefresher,
]:
    """Create refreshers for one incremental run."""
    return {
        "deals": HubSpotDealIncrementalRefresher(),
        "companies": HubSpotCompanyIncrementalRefresher(),
        "contacts": HubSpotContactIncrementalRefresher(),
        "tasks": HubSpotTaskIncrementalRefresher(),
        "notes": HubSpotNoteIncrementalRefresher(),
    }
