"""Deal summary agent workflow.

Provides the DealSummaryAgent for generating comprehensive summaries
of deals by using tools to analyze stakeholders, meetings, and emails.
"""

from src.agents.workflows.deal_summary.agent import DealSummaryAgent
from src.agents.workflows.deal_summary.models import DealSummaryData, DealSummaryInput

__all__ = ["DealSummaryAgent", "DealSummaryData", "DealSummaryInput"]
