"""CRM-related agent tools.

Tools for accessing stakeholder, deal, and risk/blocker information.
"""

from src.agents.tools.crm.get_risks_blockers import create_get_existing_risks_blockers
from src.agents.tools.crm.get_stakeholder_details import create_get_stakeholder_details
from src.agents.tools.crm.list_stakeholders import create_list_deal_stakeholders

__all__ = [
    "create_list_deal_stakeholders",
    "create_get_stakeholder_details",
    "create_get_existing_risks_blockers",
]
