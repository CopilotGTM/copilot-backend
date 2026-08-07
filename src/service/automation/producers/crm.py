"""Canonical CRM lifecycle events built from the immutable mutation ledger."""

from __future__ import annotations

from uuid import UUID

from src.domain.models.organisation_tables.sync.crm.deals import CRMDealMutationEvent
from src.service.automation.events.contracts import AutomationEventEnvelope
from src.service.automation.triggers.registry import AUTOMATION_REGISTRY

_SUPPORTED_ENTITIES = {"deal", "contact", "account"}


class CRMEventAdapter:
    """Map one stored CRM mutation to one base lifecycle envelope."""

    def __init__(self, organisation_id: UUID) -> None:
        self.organisation_id = organisation_id

    def from_mutation(self, event: CRMDealMutationEvent) -> AutomationEventEnvelope | None:
        """Build the canonical event or contract from mutation."""
        if event.organisation_id != self.organisation_id:
            raise ValueError("CRM mutation organisation does not match adapter")
        if event.entity_type not in _SUPPORTED_ENTITIES:
            return None
        lifecycle = self._lifecycle(event.event_type)
        event_key = f"crm.{event.entity_type}.{lifecycle}"
        actor_type = (
            "agent"
            if event.source in {"agent", "agent_tool", "workflow"}
            else "user"
            if event.actor_user_id is not None
            else "integration"
            if event.integration_id is not None
            else "system"
        )
        actor_id = None if actor_type == "agent" else event.actor_user_id or event.integration_id
        changes: dict[str, dict[str, object]] = {}
        if lifecycle == "updated":
            event_definition = AUTOMATION_REGISTRY.event(event_key)
            # The mutation ledger keeps audit-only fields. Automation exposes only
            # matcher fields, plus deal custom fields validated by the tenant registry.
            for key, value in event.changed_fields.items():
                # Deal rows call this relation company_id; the public automation
                # schema calls the same business object account_id.
                event_field = (
                    "account_id" if event.entity_type == "deal" and key == "company_id" else key
                )
                if event_field in event_definition.watch_fields or (
                    event.entity_type == "deal" and event_field.startswith("custom_properties.")
                ):
                    changes[event_field] = {
                        "before": value["old"],
                        "after": value["new"],
                    }
            if not changes:
                return None
        return AutomationEventEnvelope.model_validate(
            {
                "event_id": event.event_id,
                "organisation_id": self.organisation_id,
                "event_key": event_key,
                "route": {"mode": "match"},
                "occurred_at": event.occurred_at,
                "source": {
                    "domain": "crm",
                    "system": event.provider or event.source,
                    "operation": lifecycle.removesuffix("d"),
                    "mode": event.automation_source_mode,
                    "source_event_id": str(event.event_id),
                },
                "actor": {
                    "type": actor_type,
                    "id": str(actor_id) if actor_id else None,
                },
                "subject": {
                    "type": event.entity_type,
                    "id": str(event.entity_id),
                },
                "record": event.record_snapshot,
                "changes": changes,
                "correlation_id": event.correlation_id or str(event.mutation_group_id),
                "caused_by_run_id": event.caused_by_run_id,
                "causation_depth": event.causation_depth,
                "idempotency_key": f"{event_key}:{event.event_id}",
            }
        )

    @staticmethod
    def _lifecycle(event_type: str) -> str:
        suffix = event_type.rsplit(".", 1)[-1]
        return suffix if suffix in {"created", "deleted"} else "updated"


__all__ = ["CRMEventAdapter"]
