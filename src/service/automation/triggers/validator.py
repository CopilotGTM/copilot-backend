"""Compile and validate user trigger decisions against the shared registry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import ValidationError
from src.api.schemas.automation_trigger_models import AutomationTriggerDefinitionBase
from src.domain.models.organisation_tables import (
    AutomationAudienceMode,
    AutomationPrincipalType,
    AutomationScopeType,
)
from src.service.automation.config_validation import contains_credentials, validate_json_document
from src.service.automation.triggers.capabilities import RETRY_UNSAFE_AUTOMATION_TOOLS
from src.service.automation.triggers.matcher import (
    collect_option_selections,
    combine_filter_trees,
    filter_tree_to_dict,
    validate_filter_tree,
    validate_watch,
)
from src.service.automation.triggers.registry import (
    AUTOMATION_REGISTRY,
    AutomationRegistry,
    TriggerDefinition,
)
from src.service.automation.triggers.schedule import normalize_schedule_config
from src.service.automation.triggers.webhook import validate_webhook_payload_schema

# Keep user-authored trigger configuration small enough to hash, persist, and
# include in pinned run snapshots without allowing unbounded JSON input.
_CONFIG_MAX_BYTES = 16_384


class TriggerOptionValidator(Protocol):
    """Tenant-owned values needed to validate stable IDs in saved bindings."""

    async def validate_option_selection(self, source: str, values: set[str]) -> None:
        """Reject filter or preset option values outside the current organization."""

        ...

    async def validate_audience_user_ids(self, user_ids: set[UUID]) -> None:
        """Reject inactive, unapproved, or foreign audience users."""

        ...

    async def validate_schedule_subjects(
        self,
        subject_type: str,
        subject_ids: set[UUID],
        *,
        principal_id: UUID,
        organisation_scope: bool,
    ) -> None:
        """Reject scheduled records outside the saved execution authority."""

        ...


@dataclass(frozen=True)
class ValidatedTriggerDefinition:
    """Canonical server projections ready for hashing and persistence."""

    definition: TriggerDefinition
    trigger_config: dict[str, Any]
    watch_config: dict[str, Any] | None
    filter_config: dict[str, Any] | None
    execution_principal_type: AutomationPrincipalType
    execution_principal_id: UUID
    scope_type: AutomationScopeType
    scope_config: dict[str, Any]
    audience_mode: AutomationAudienceMode
    audience_config: dict[str, Any]
    task_config: dict[str, Any]
    delivery_config: dict[str, Any]
    next_trigger_at: datetime | None


class AutomationTriggerValidator:
    """Turn one decision-only request into a complete fail-closed binding."""

    def __init__(
        self,
        options: TriggerOptionValidator,
        *,
        organisation_id: UUID,
        user_id: UUID,
        is_admin: bool,
        registry: AutomationRegistry = AUTOMATION_REGISTRY,
    ) -> None:
        self.options = options
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.is_admin = is_admin
        self.registry = registry

    async def validate(
        self,
        request: AutomationTriggerDefinitionBase,
        *,
        target_allowed_tools: tuple[str, ...] | list[str] = (),
        target_allowed_skills: tuple[str, ...] | list[str] = (),
        now: datetime | None = None,
    ) -> ValidatedTriggerDefinition:
        """Validate, compile, normalize, and derive every server-owned projection."""

        now = (now or datetime.now(UTC)).astimezone(UTC)
        self._validate_json(request)
        definition = self.registry.trigger(request.trigger_key)
        if not definition.producer_available or any(
            not self.registry.event(key).producer_available for key in definition.match_event_keys
        ):
            raise ValueError("trigger producer is not available")

        try:
            compiled = definition.compile(request.config)
            events = tuple(self.registry.event(key) for key in definition.match_event_keys)
            if request.watch is not None and not definition.allow_user_watch:
                raise ValueError("trigger does not accept a user watch")
            selected_watch = request.watch.model_dump() if request.watch else compiled.watch
            watch = validate_watch(selected_watch, events)
            combined_filter = combine_filter_trees(compiled.filter, request.filter)
            filter_node = validate_filter_tree(combined_filter, events)
        except ValidationError as exc:
            raise ValueError(exc.errors()[0]["msg"]) from exc

        for source, values in collect_option_selections(
            definition,
            compiled.config,
            filter_node,
        ).items():
            await self.options.validate_option_selection(source, values)
        principal_type, principal_id, audience_config = await self._validate_authority(
            request,
            target_allowed_tools=target_allowed_tools,
            target_allowed_skills=target_allowed_skills,
        )

        trigger_config = dict(compiled.config)
        next_trigger_at = None
        if definition.family.value == "schedule":
            trigger_config, next_trigger_at = self._validate_schedule(
                trigger_config,
                now,
            )
            if subject_type := trigger_config.get("subject_type"):
                await self.options.validate_schedule_subjects(
                    subject_type,
                    {UUID(item) for item in trigger_config["subject_ids"]},
                    principal_id=principal_id,
                    organisation_scope=(
                        principal_type == AutomationPrincipalType.ORGANISATION_SERVICE
                    ),
                )
        elif definition.family.value == "webhook":
            self._validate_webhook(trigger_config)

        return ValidatedTriggerDefinition(
            definition=definition,
            trigger_config=trigger_config,
            watch_config=(
                {"mode": watch.mode, "paths": list(watch.paths)} if watch is not None else None
            ),
            filter_config=filter_tree_to_dict(filter_node),
            execution_principal_type=principal_type,
            execution_principal_id=principal_id,
            scope_type=request.scope.type,
            scope_config=dict(request.scope.config),
            audience_mode=request.audience.mode,
            audience_config=audience_config,
            task_config=request.task.model_dump(exclude_none=True),
            delivery_config=request.delivery.model_dump(),
            next_trigger_at=next_trigger_at,
        )

    @staticmethod
    def _validate_json(request: AutomationTriggerDefinitionBase) -> None:
        values = {
            "config": request.config,
            "watch": request.watch.model_dump() if request.watch else None,
            "filter": request.filter,
            "scope": request.scope.model_dump(mode="json"),
            "audience": request.audience.model_dump(mode="json"),
            "task": request.task.model_dump(),
            "delivery": request.delivery.model_dump(),
        }
        for field_name, value in values.items():
            validate_json_document(field_name, value, max_bytes=_CONFIG_MAX_BYTES)
            if contains_credentials(value):
                raise ValueError(f"{field_name} must not contain credentials")

    async def _validate_authority(
        self,
        request: AutomationTriggerDefinitionBase,
        *,
        target_allowed_tools: tuple[str, ...] | list[str],
        target_allowed_skills: tuple[str, ...] | list[str],
    ) -> tuple[AutomationPrincipalType, UUID, dict[str, Any]]:
        definition = self.registry.trigger(request.trigger_key)
        scope_type = request.scope.type.value
        audience_mode = request.audience.mode.value
        if request.principal_mode not in definition.allowed_principal_modes:
            raise ValueError("principal mode is not allowed for this trigger")
        if scope_type not in definition.allowed_scope_types:
            raise ValueError("scope type is not allowed for this trigger")
        if audience_mode not in definition.allowed_audience_modes:
            raise ValueError("audience mode is not allowed for this trigger")
        if request.scope.config:
            raise ValueError("scope config is not supported by this trigger")
        if set(target_allowed_tools) & RETRY_UNSAFE_AUTOMATION_TOOLS:
            raise ValueError("automation mutating tools are disabled until actions are retry-safe")

        if request.principal_mode == "personal":
            if (
                request.scope.type != AutomationScopeType.OWNED_OR_CREATED_BY_PRINCIPAL
                or request.audience.mode != AutomationAudienceMode.PRINCIPAL
            ):
                raise ValueError("personal bindings require personal scope and principal audience")
            return AutomationPrincipalType.USER, self.user_id, {}

        if not self.is_admin:
            raise ValueError("organisation bindings require an organisation admin")
        if request.scope.type != AutomationScopeType.ORGANISATION or request.audience.mode not in {
            AutomationAudienceMode.NONE,
            AutomationAudienceMode.EXPLICIT_USERS,
            AutomationAudienceMode.SUBJECT_PARTICIPANTS,
        }:
            raise ValueError("organisation bindings require organisation scope and audience")
        if request.audience.mode == AutomationAudienceMode.SUBJECT_PARTICIPANTS and not all(
            self.registry.event(key).supports_subject_participants
            for key in definition.match_event_keys
        ):
            raise ValueError("subject participant audience is not supported for this trigger")
        if target_allowed_tools:
            # copilotgtm: Organization service agents stay analysis-only until
            # each tool has a code-owned service-principal capability policy.
            raise ValueError("organisation service agents cannot use user-scoped tools")
        if target_allowed_skills:
            raise ValueError("organisation service agents cannot use skills")
        audience_ids = set(request.audience.user_ids)
        if audience_ids:
            await self.options.validate_audience_user_ids(audience_ids)
        return (
            AutomationPrincipalType.ORGANISATION_SERVICE,
            self.organisation_id,
            {"user_ids": sorted(str(item) for item in audience_ids)} if audience_ids else {},
        )

    @classmethod
    def _validate_schedule(
        cls,
        value: dict[str, Any],
        now: datetime,
    ) -> tuple[dict[str, Any], datetime]:
        return normalize_schedule_config(value, now=now)

    @staticmethod
    def _validate_webhook(config: dict[str, Any]) -> None:
        validate_webhook_payload_schema(config.get("payload_schema"))


__all__ = ["AutomationTriggerValidator", "ValidatedTriggerDefinition"]
