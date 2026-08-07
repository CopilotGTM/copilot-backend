"""Reconcile saved automation bindings after CRM schema changes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables import (
    AutomationTrigger,
    AutomationTriggerStatus,
)
from src.infra.utils.logger import get_logger
from src.service.automation.triggers.crm_schema import AutomationCRMRegistryService
from src.service.automation.triggers.matcher import (
    MatcherValidationError,
    validate_filter_tree,
    validate_watch,
)
from src.service.automation.triggers.registry import AutomationRegistry

logger = get_logger(__name__)

_DEAL_EVENT_KEYS = ("crm.deal.created", "crm.deal.updated", "crm.deal.deleted")


class AutomationTriggerSchemaReconciliationService:
    """Pause active deal bindings affected by a breaking custom-field change."""

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.registry_service = AutomationCRMRegistryService(session, organisation_id)

    async def reconcile_custom_field_changes(self, field_keys: set[str]) -> int:
        """Revalidate and pause only bindings that reference the changed paths."""

        normalized = {key.strip() for key in field_keys if key and key.strip()}
        if not normalized:
            return 0
        candidates = await self._list_active_deal_bindings()
        affected = [
            trigger
            for trigger in candidates
            if self._references_any_custom_field(trigger, normalized)
        ]
        if not affected:
            return 0

        registry = (await self.registry_service.snapshot()).registry
        for trigger in affected:
            try:
                self._validate_saved_matcher(trigger, registry)
            except (MatcherValidationError, ValueError):
                error = (
                    "A custom field used by this trigger was removed or is no longer "
                    "compatible with its matcher. Review it before reactivating."
                )
            else:
                # A declared type change must not silently reinterpret a saved literal
                # even when its new type happens to accept the old JSON value.
                error = (
                    "A custom field used by this trigger changed type. "
                    "Review the matcher before reactivating it."
                )
            trigger.status = AutomationTriggerStatus.PAUSED
            trigger.next_trigger_at = None
            trigger.last_error_code = "custom_field_schema_changed"
            trigger.last_error = error
            trigger.updated_at = datetime.now(UTC)
        await self.session.flush()
        logger.info(
            "Paused automation bindings after CRM schema change org=%s fields=%s count=%d",
            self.organisation_id,
            sorted(normalized),
            len(affected),
        )
        return len(affected)

    async def _list_active_deal_bindings(self) -> list[AutomationTrigger]:
        columns = cast(Any, AutomationTrigger).__table__.c
        # copilotgtm: This bounded-by-tenant active-binding scan keeps matching exact;
        # add a JSON path reference index if organizations grow thousands of bindings.
        statement = select(AutomationTrigger).where(
            columns.organisation_id == self.organisation_id,
            columns.status == AutomationTriggerStatus.ACTIVE.value,
            columns.family == "event",
            columns.match_event_keys.overlap(list(_DEAL_EVENT_KEYS)),
        )
        return list((await self.session.execute(statement)).scalars().all())

    @classmethod
    def _references_any_custom_field(
        cls,
        trigger: AutomationTrigger,
        field_keys: set[str],
    ) -> bool:
        paths = set(cls._filter_paths(trigger.filter_config))
        if trigger.watch_config and isinstance(trigger.watch_config.get("paths"), list):
            paths.update(path for path in trigger.watch_config["paths"] if isinstance(path, str))
        for key in field_keys:
            bases = (
                f"custom_properties.{key}",
                f"record.custom_properties.{key}",
                f"changes.custom_properties.{key}",
            )
            if any(path == base or path.startswith(f"{base}.") for path in paths for base in bases):
                return True
        return False

    @classmethod
    def _filter_paths(cls, value: Any) -> list[str]:
        if not isinstance(value, dict):
            return []
        paths = [value["path"]] if isinstance(value.get("path"), str) else []
        for key in ("all", "any"):
            children = value.get(key)
            if isinstance(children, list):
                for child in children:
                    paths.extend(cls._filter_paths(child))
        return paths

    @staticmethod
    def _validate_saved_matcher(
        trigger: AutomationTrigger,
        registry: AutomationRegistry,
    ) -> None:
        definition = registry.trigger(trigger.trigger_key)
        if tuple(trigger.match_event_keys) != definition.match_event_keys:
            raise ValueError("stored trigger events no longer match the registry")
        events = tuple(registry.event(key) for key in trigger.match_event_keys)
        validate_watch(trigger.watch_config, events)
        validate_filter_tree(trigger.filter_config, events)
