"""CRM-related agent tools.

Tools for accessing deal, stakeholder, contact, and risk/blocker information.
"""

from src.agents.tools.crm.add_deal_note import create_add_deal_note
from src.agents.tools.crm.add_stakeholder import create_add_stakeholder
from src.agents.tools.crm.create_deal import create_create_deal
from src.agents.tools.crm.enrich_stakeholder import create_enrich_stakeholder
from src.agents.tools.crm.get_deal_change_history import create_get_deal_change_history
from src.agents.tools.crm.get_deal_details import create_get_deal_details
from src.agents.tools.crm.get_deal_memory import create_get_deal_memory
from src.agents.tools.crm.get_deal_summary import create_get_deal_summary
from src.agents.tools.crm.get_pipeline_details import create_get_pipeline_details
from src.agents.tools.crm.get_risks_blockers import create_get_existing_risks_blockers
from src.agents.tools.crm.get_stakeholder_details import create_get_stakeholder_details
from src.agents.tools.crm.list_deal_notes import create_list_deal_notes
from src.agents.tools.crm.list_deals import create_list_deals
from src.agents.tools.crm.list_stakeholders import create_list_deal_stakeholders
from src.agents.tools.crm.manage_risks_blockers import (
    create_dismiss_risk_blocker,
    create_resolve_risk_blocker,
    create_update_risk_blocker,
)
from src.agents.tools.crm.search_contacts import create_search_contacts
from src.agents.tools.crm.search_deals import create_search_deals
from src.agents.tools.crm.update_deal_stage import create_update_deal_stage
from src.agents.tools.crm.upsert_stakeholder import create_upsert_stakeholder

__all__ = [
    # Deal tools
    "create_add_deal_note",
    "create_add_stakeholder",
    "create_create_deal",
    "create_update_deal_stage",
    "create_search_deals",
    "create_list_deals",
    "create_get_deal_details",
    "create_get_deal_memory",
    "create_get_deal_summary",
    "create_get_deal_change_history",
    "create_get_pipeline_details",
    # Stakeholder tools
    "create_list_deal_stakeholders",
    "create_get_stakeholder_details",
    "create_upsert_stakeholder",
    "create_enrich_stakeholder",
    # Contact tools
    "create_search_contacts",
    # Risk/blocker tools
    "create_get_existing_risks_blockers",
    "create_update_risk_blocker",
    "create_dismiss_risk_blocker",
    "create_resolve_risk_blocker",
    # Notes and insights
    "create_list_deal_notes",
]
