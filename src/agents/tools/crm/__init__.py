"""CRM-related agent tools.

Tools for accessing deal, stakeholder, contact, and risk/blocker information.
"""

from src.agents.tools.crm.create_stakeholder import create_create_stakeholder
from src.agents.tools.crm.enrich_stakeholder import create_enrich_stakeholder
from src.agents.tools.crm.get_deal_change_history import create_get_deal_change_history
from src.agents.tools.crm.get_deal_details import create_get_deal_details
from src.agents.tools.crm.get_deal_summary import create_get_deal_summary
from src.agents.tools.crm.get_pipeline_details import create_get_pipeline_details
from src.agents.tools.crm.get_risks_blockers import create_get_existing_risks_blockers
from src.agents.tools.crm.get_stakeholder_details import create_get_stakeholder_details
from src.agents.tools.crm.list_deal_notes import create_list_deal_notes
from src.agents.tools.crm.list_deals import create_list_deals
from src.agents.tools.crm.list_stakeholder_insights import create_list_deal_stakeholder_insights
from src.agents.tools.crm.list_stakeholders import create_list_deal_stakeholders
from src.agents.tools.crm.search_contacts import create_search_contacts
from src.agents.tools.crm.search_deals import create_search_deals

__all__ = [
    # Deal tools
    "create_search_deals",
    "create_list_deals",
    "create_get_deal_details",
    "create_get_deal_summary",
    "create_get_deal_change_history",
    "create_get_pipeline_details",
    # Stakeholder tools
    "create_list_deal_stakeholders",
    "create_get_stakeholder_details",
    "create_create_stakeholder",
    "create_enrich_stakeholder",
    # Contact tools
    "create_search_contacts",
    # Risk/blocker tools
    "create_get_existing_risks_blockers",
    # Notes and insights
    "create_list_deal_notes",
    "create_list_deal_stakeholder_insights",
]
