"""Deal summary agent workflow.

Provides the DealSummaryAgent for generating comprehensive summaries
of deals by analyzing all associated meeting transcripts.
"""

from src.agents.workflows.deal_summary.agent import DealSummaryAgent
from src.agents.workflows.deal_summary.models import DealSummaryData

__all__ = ["DealSummaryAgent", "DealSummaryData"]
