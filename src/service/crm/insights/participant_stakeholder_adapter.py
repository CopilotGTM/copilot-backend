"""Compatibility re-export for participant-backed stakeholder adapter."""

from src.service.crm.participants.stakeholder_adapter import (
    participant_to_stakeholder_detail_dict,
)

__all__ = ["participant_to_stakeholder_detail_dict"]
