"""Shared calendar integration type contracts.

User-facing meeting queries are scoped through provider-parent integration rows
because `CalendarEventUserState.integration_id` records the row that synced the
event. Dispatch keeps capability rows eligible so older data shapes still pass
the final meeting-bot gate during rollout.
"""

from __future__ import annotations

from src.domain.models.organisation_tables.sync.integration_models import IntegrationType

PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPES = (
    IntegrationType.GOOGLE,
    IntegrationType.MICROSOFT,
)

PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPE_VALUES = tuple(
    integration_type.value for integration_type in PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPES
)

DISPATCH_CALENDAR_INTEGRATION_TYPES = (
    IntegrationType.GOOGLE,
    IntegrationType.MICROSOFT,
    IntegrationType.GOOGLE_CALENDAR,
    IntegrationType.OUTLOOK_CALENDAR,
)
