"""Stakeholder change types for CRM sync.

This module defines the change types that trigger stakeholder sync
and calendar/email re-matching.
"""

from enum import Enum


class StakeholderChangeType(str, Enum):
    """Types of changes that trigger stakeholder sync.

    Used by:
    - CRM sync service (to dispatch stakeholder task)
    - Stakeholder task (to determine if re-matching needed)

    Values:
    - NEW_DEAL: New deal created, sync all stakeholders
    - PARTICIPANT_ADDED: New contact added to deal (unused currently)
    - PARTICIPANT_UPDATED: Contact email changed (unused currently)
    - PARTICIPANT_REMOVED: Contact removed from deal (unused currently)
    - MANUAL_REFRESH: User-triggered refresh
    """

    NEW_DEAL = "NEW_DEAL"
    PARTICIPANT_ADDED = "PARTICIPANT_ADDED"
    PARTICIPANT_UPDATED = "PARTICIPANT_UPDATED"
    PARTICIPANT_REMOVED = "PARTICIPANT_REMOVED"
    MANUAL_REFRESH = "MANUAL_REFRESH"
