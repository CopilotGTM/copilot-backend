"""Lifecycle service for user-owned triggers pinned to executable agent revisions."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.schemas.automation_trigger_models import (
    AutomationTriggerCatalog,
    AutomationTriggerCreateRequest,
    AutomationTriggerDetail,
    AutomationTriggerListResponse,
    AutomationTriggerUpdateRequest,
)
from src.domain.models.organisation_tables import (
    AgentStatus,
    AutomationScopeType,
    AutomationTargetType,
    AutomationTrigger,
    AutomationTriggerStatus,
)
from src.infra.utils.logger import get_logger
from src.service.agents.serializers import agent_execution_snapshot
from src.service.automation.errors import AutomationServiceError
from src.service.automation.triggers.access import AutomationTriggerAccess
from src.service.automation.triggers.catalog import build_trigger_catalog
from src.service.automation.triggers.options import AutomationTriggerOptionAccess
from src.service.automation.triggers.serializers import trigger_detail, trigger_summary
from src.service.automation.triggers.validator import AutomationTriggerValidator

logger = get_logger("automation_trigger_service")


class AutomationTriggerService:
    """Manage trigger definitions for one authenticated user's owned agents."""

    def __init__(self, session: AsyncSession, organisation_id: UUID, owner_user_id: UUID) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.owner_user_id = owner_user_id
        self.access = AutomationTriggerAccess(session, organisation_id, owner_user_id)
        self.options = AutomationTriggerOptionAccess(session, organisation_id)
        self.validator = AutomationTriggerValidator(self.options)

    async def close(self) -> None:
        """Release the request-scoped database session."""

        try:
            await self.session.close()
        except Exception:
            logger.debug("AutomationTriggerService close failed", exc_info=True)

    async def get_catalog(self) -> AutomationTriggerCatalog:
        """Return exact supported trigger contracts plus organization CRM options."""

        return build_trigger_catalog(await self.options.list_pipelines())

    async def create_trigger(
        self, agent_id: UUID, body: AutomationTriggerCreateRequest
    ) -> AutomationTriggerDetail:
        """Validate and pin a new trigger to the agent's current executable revision."""

        agent = await self._get_agent(agent_id)
        self._require_activatable(agent.status, body.status)
        try:
            validated = await self.validator.validate(body)
        except ValueError as exc:
            raise AutomationServiceError(str(exc)) from exc
        snapshot = agent_execution_snapshot(agent)
        config_hash = self._config_hash(
            agent.revision,
            body,
            validated.trigger_config,
            validated.filter_config,
            validated.scope_config,
            validated.task_config,
        )
        if await self.access.find_duplicate(agent_id, config_hash):
            raise AutomationServiceError("An equivalent trigger already exists", status_code=409)
        trigger = AutomationTrigger(
            organisation_id=self.organisation_id,
            owner_user_id=self.owner_user_id,
            name=body.name,
            target_type=AutomationTargetType.AGENT,
            target_id=agent_id,
            target_revision=agent.revision,
            target_snapshot=snapshot,
            trigger_type=body.trigger_type,
            source_domain=body.source_domain,
            event_type=body.event_type,
            config_schema_version=1,
            trigger_config=validated.trigger_config,
            filter_config=validated.filter_config,
            scope_type=AutomationScopeType.OWNER,
            scope_config=validated.scope_config,
            task_config=validated.task_config,
            delivery_config={},
            config_hash=config_hash,
            status=body.status,
            next_trigger_at=validated.next_trigger_at,
        )
        try:
            await self.access.create_trigger(trigger)
        except IntegrityError as exc:
            raise AutomationServiceError(
                "An equivalent trigger already exists", status_code=409
            ) from exc
        return trigger_detail(trigger, current_target_revision=agent.revision)

    async def list_triggers(
        self,
        agent_id: UUID,
        *,
        status: AutomationTriggerStatus | None,
        limit: int,
        offset: int,
    ) -> AutomationTriggerListResponse:
        """Return one page of the current owner's non-archived agent triggers."""

        if status == AutomationTriggerStatus.ARCHIVED:
            raise AutomationServiceError("Archived triggers are not available from the user list")
        agent = await self._get_agent(agent_id)
        rows, total = await self.access.list_triggers(
            agent_id, status=status, limit=limit, offset=offset
        )
        return AutomationTriggerListResponse(
            items=[trigger_summary(row, current_target_revision=agent.revision) for row in rows],
            total_count=total,
            limit=limit,
            offset=offset,
        )

    async def get_trigger(self, agent_id: UUID, trigger_id: UUID) -> AutomationTriggerDetail:
        """Return one owned trigger and whether its agent pin is stale."""

        agent = await self._get_agent(agent_id)
        trigger = await self.access.get_trigger(agent_id, trigger_id)
        if trigger is None:
            raise AutomationServiceError("Trigger not found", status_code=404)
        return trigger_detail(trigger, current_target_revision=agent.revision)

    async def replace_trigger(
        self, agent_id: UUID, trigger_id: UUID, body: AutomationTriggerUpdateRequest
    ) -> AutomationTriggerDetail:
        """Replace configurable fields while preserving the explicit target revision pin."""

        agent = await self._get_agent(agent_id)
        self._require_activatable(agent.status, body.status)
        current = await self.access.get_trigger(agent_id, trigger_id)
        if current is None:
            raise AutomationServiceError("Trigger not found", status_code=404)
        try:
            validated = await self.validator.validate(body)
        except ValueError as exc:
            raise AutomationServiceError(str(exc)) from exc
        config_hash = self._config_hash(
            current.target_revision,
            body,
            validated.trigger_config,
            validated.filter_config,
            validated.scope_config,
            validated.task_config,
        )
        if await self.access.find_duplicate(agent_id, config_hash, exclude_trigger_id=trigger_id):
            raise AutomationServiceError("An equivalent trigger already exists", status_code=409)
        updated = await self.access.replace_trigger(
            agent_id,
            trigger_id,
            expected_updated_at=body.expected_updated_at,
            values={
                "name": body.name,
                "status": body.status.value,
                "trigger_type": body.trigger_type.value,
                "source_domain": body.source_domain,
                "event_type": body.event_type,
                "trigger_config": validated.trigger_config,
                "filter_config": validated.filter_config,
                "scope_config": validated.scope_config,
                "task_config": validated.task_config,
                "next_trigger_at": validated.next_trigger_at,
                "config_hash": config_hash,
            },
        )
        if updated is None:
            await self._raise_conditional_failure(agent_id, trigger_id, body.expected_updated_at)
        return trigger_detail(updated, current_target_revision=agent.revision)

    async def refresh_target(
        self, agent_id: UUID, trigger_id: UUID, *, expected_updated_at: datetime
    ) -> AutomationTriggerDetail:
        """Explicitly repin one trigger to the agent's current executable revision."""

        agent = await self._get_agent(agent_id)
        current = await self.access.get_trigger(agent_id, trigger_id)
        if current is None:
            raise AutomationServiceError("Trigger not found", status_code=404)
        self._require_activatable(agent.status, current.status)
        config_hash = self._stored_config_hash(current, target_revision=agent.revision)
        if await self.access.find_duplicate(agent_id, config_hash, exclude_trigger_id=trigger_id):
            raise AutomationServiceError("An equivalent trigger already exists", status_code=409)
        updated = await self.access.replace_trigger(
            agent_id,
            trigger_id,
            expected_updated_at=expected_updated_at,
            values={
                "target_revision": agent.revision,
                "target_snapshot": agent_execution_snapshot(agent),
                "config_hash": config_hash,
            },
        )
        if updated is None:
            await self._raise_conditional_failure(agent_id, trigger_id, expected_updated_at)
        return trigger_detail(updated, current_target_revision=agent.revision)

    async def archive_trigger(
        self, agent_id: UUID, trigger_id: UUID, *, expected_updated_at: datetime
    ) -> None:
        """Archive one trigger while retaining execution and audit references."""

        await self._get_agent(agent_id)
        archived = await self.access.archive_trigger(
            agent_id, trigger_id, expected_updated_at=expected_updated_at
        )
        if archived is None:
            await self._raise_conditional_failure(agent_id, trigger_id, expected_updated_at)

    async def _get_agent(self, agent_id: UUID) -> Any:
        agent = await self.access.get_agent(agent_id)
        if agent is None:
            raise AutomationServiceError("Agent not found", status_code=404)
        return agent

    @staticmethod
    def _require_activatable(
        agent_status: AgentStatus, trigger_status: AutomationTriggerStatus
    ) -> None:
        if trigger_status == AutomationTriggerStatus.ACTIVE and agent_status != AgentStatus.ACTIVE:
            raise AutomationServiceError(
                "An active trigger requires an active agent", status_code=409
            )

    async def _raise_conditional_failure(
        self, agent_id: UUID, trigger_id: UUID, expected_updated_at: datetime
    ) -> NoReturn:
        current = await self.access.get_trigger(agent_id, trigger_id, include_archived=True)
        if current is None or current.status == AutomationTriggerStatus.ARCHIVED:
            raise AutomationServiceError("Trigger not found", status_code=404)
        if current.updated_at != expected_updated_at:
            raise AutomationServiceError(
                "Trigger changed since it was loaded; refresh and retry", status_code=409
            )
        raise AutomationServiceError("Trigger could not be updated", status_code=409)

    @classmethod
    def _config_hash(
        cls,
        target_revision: int,
        body: Any,
        trigger_config: dict[str, Any],
        filter_config: dict[str, Any],
        scope_config: dict[str, Any],
        task_config: dict[str, Any],
    ) -> str:
        return cls._hash_payload(
            {
                "target_revision": target_revision,
                "trigger_type": body.trigger_type.value,
                "source_domain": body.source_domain,
                "event_type": body.event_type,
                "trigger_config": trigger_config,
                "filter_config": filter_config,
                "scope_type": AutomationScopeType.OWNER.value,
                "scope_config": scope_config,
                "task_config": task_config,
            }
        )

    @classmethod
    def _stored_config_hash(cls, trigger: AutomationTrigger, *, target_revision: int) -> str:
        return cls._hash_payload(
            {
                "target_revision": target_revision,
                "trigger_type": cls._enum_value(trigger.trigger_type),
                "source_domain": trigger.source_domain,
                "event_type": trigger.event_type,
                "trigger_config": trigger.trigger_config,
                "filter_config": trigger.filter_config,
                "scope_type": cls._enum_value(trigger.scope_type),
                "scope_config": trigger.scope_config,
                "task_config": trigger.task_config,
            }
        )

    @staticmethod
    def _enum_value(value: Any) -> Any:
        """Handle ORM rows and UPDATE RETURNING rows consistently."""

        return getattr(value, "value", value)

    @staticmethod
    def _hash_payload(value: dict[str, Any]) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
