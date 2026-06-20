"""Compatibility exports for risk/blocker source resolver helpers."""

from src.service.crm.risks.source_resolver import (
    resolve_email_risk_blocker_source,
    resolve_meeting_risk_blocker_source,
)

__all__ = [
    "resolve_email_risk_blocker_source",
    "resolve_meeting_risk_blocker_source",
]
