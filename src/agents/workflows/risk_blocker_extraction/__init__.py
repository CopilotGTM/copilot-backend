"""Risk/Blocker extraction workflow package.

This package provides the agent and models for extracting risks and blockers
from deal communications (meetings, emails, Slack messages).
"""

from src.agents.workflows.risk_blocker_extraction.agent import (
    RISK_BLOCKER_PROMPT_VERSION,
    RiskBlockerExtractionAgent,
)
from src.agents.workflows.risk_blocker_extraction.models import (
    ExtractedRiskBlocker,
    RiskBlockerExtractionResult,
)

__all__ = [
    "RiskBlockerExtractionAgent",
    "RISK_BLOCKER_PROMPT_VERSION",
    "ExtractedRiskBlocker",
    "RiskBlockerExtractionResult",
]
