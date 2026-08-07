"""Lifecycle service for registry-backed triggers pinned to agent revisions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, NoReturn, cast
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.schemas.automation_trigger_models import (
    AutomationManualInvocationRequest,
    AutomationManualInvocationResponse,
    AutomationManualInvocationSubjectResult,
    AutomationTriggerCatalog,
    AutomationTriggerCreateRequest,
    AutomationTriggerDetail,
    AutomationTriggerListResponse,
    AutomationTriggerUpdateRequest,
    AutomationWebhookCredentials,
    AutomationWebhookRotateRequest,
)
from src.domain.models.organisation_tables import (
    AgentStatus,
    AutomationPrincipalType,
    AutomationTargetType,
    AutomationTrigger,
    AutomationTriggerFamily,
    AutomationTriggerStatus,
)
from src.infra.config.configs import security_secrets_config
from src.infra.utils.logger import get_logger
from src.service.agents.serializers import agent_execution_snapshot
from src.service.automation.errors import AutomationServiceError
from src.service.automation.triggers.access import AutomationTriggerAccess
from src.service.automation.triggers.capabilities import RETRY_UNSAFE_AUTOMATION_TOOLS
from src.service.automation.triggers.catalog import build_trigger_catalog
from src.service.automation.triggers.crm_schema import AutomationCRMRegistryService
from src.service.automation.triggers.direct import ManualInvocationService, ManualSubject
from src.service.automation.triggers.options import AutomationTriggerOptionAccess
from src.service.automation.triggers.serializers import trigger_detail, trigger_summary
from src.service.automation.triggers.validator import (
    AutomationTriggerValidator,
    ValidatedTriggerDefinition,
)
from src.service.automation.triggers.webhook import (
    provision_webhook_config,
    public_webhook_config,
    retain_webhook_credentials,
    rotate_webhook_secret,
    webhook_config_is_provisioned,
)

logger = get_logger("automation_trigger_service")


class AutomationTriggerService:
    """Manage compiled bindings for one authenticated agent owner."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        created_by_user_id: UUID,
        *,
        is_admin: bool = False,
        webhook_root_secret_provider: Callable[[], Awaitable[str]] = (
            security_secrets_config.get_automation_webhook_root_secret
        ),
        webhook_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.created_by_user_id = created_by_user_id
        self.is_admin = is_admin
        self.webhook_root_secret_provider = webhook_root_secret_provider
        self.webhook_clock = webhook_clock
        self.access = AutomationTriggerAccess(
            session,
            organisation_id,
            created_by_user_id,
            is_admin=is_admin,
        )
        self.options = AutomationTriggerOptionAccess(session, organisation_id)
        self.registry_service = AutomationCRMRegistryService(session, organisation_id)
        self.validator = AutomationTriggerValidator(
            self.options,
            organisation_id=organisation_id,
            user_id=created_by_user_id,
            is_admin=is_admin,
        )

    async def close(self) -> None:
        """Release the request-scoped database session."""

        try:
            await self.session.close()
        except Exception:
            logger.debug("AutomationTriggerService close failed", exc_info=True)

    async def get_catalog(self) -> AutomationTriggerCatalog:
        """Return live registry definitions plus organization-owned options."""

        snapshot = await self.registry_service.snapshot()
        return build_trigger_catalog(
            await self.options.list_pipelines(),
            registry=snapshot.registry,
            custom_option_sets=snapshot.option_sets,
        )

    async def create_trigger(
        self,
        agent_id: UUID,
        body: AutomationTriggerCreateRequest,
    ) -> AutomationTriggerDetail:
        """Compile one decision-only request and pin the current agent revision."""

        agent = await self._get_agent(agent_id)
        self._require_activatable(agent.status, body.status)
        validated = await self._validate(
            body,
            target_allowed_tools=agent.allowed_tools,
            target_allowed_skills=agent.allowed_skills,
        )
        self._require_personal_target_owner(agent, validated)
        issued_webhook_credentials = None
        trigger_id = uuid4()
        if validated.definition.family.value == "webhook":
            internal_config, issued_webhook_credentials = provision_webhook_config(
                validated.trigger_config,
                organisation_id=self.organisation_id,
                root_secret=await self._webhook_root_secret(),
                now=self.webhook_clock(),
            )
            validated = replace(validated, trigger_config=internal_config)
        snapshot = agent_execution_snapshot(agent)
        config_hash = self._config_hash(
            target_id=agent_id,
            target_revision=agent.revision,
            validated=validated,
        )
        if await self.access.find_duplicate(agent_id, config_hash):
            raise AutomationServiceError("An equivalent trigger already exists", status_code=409)
        trigger = AutomationTrigger(
            trigger_id=trigger_id,
            organisation_id=self.organisation_id,
            created_by_user_id=self.created_by_user_id,
            name=body.name,
            target_type=AutomationTargetType.AGENT,
            target_id=agent_id,
            target_revision=agent.revision,
            target_snapshot=snapshot,
            trigger_key=validated.definition.trigger_key,
            family=AutomationTriggerFamily(validated.definition.family.value),
            match_event_keys=list(validated.definition.match_event_keys),
            config_schema_version=1,
            trigger_config=validated.trigger_config,
            watch_config=validated.watch_config,
            filter_config=validated.filter_config,
            execution_principal_type=validated.execution_principal_type,
            execution_principal_id=validated.execution_principal_id,
            scope_type=validated.scope_type,
            scope_config=validated.scope_config,
            audience_mode=validated.audience_mode,
            audience_config=validated.audience_config,
            task_config=validated.task_config,
            delivery_config=validated.delivery_config,
            config_hash=config_hash,
            status=body.status,
            next_trigger_at=validated.next_trigger_at,
        )
        try:
            await self.access.create_trigger(trigger)
        except IntegrityError as exc:
            raise AutomationServiceError(
                "An equivalent trigger already exists",
                status_code=409,
            ) from exc
        detail = trigger_detail(trigger, current_target_revision=agent.revision)
        if issued_webhook_credentials is not None:
            detail.webhook_credentials = AutomationWebhookCredentials(
                endpoint_token=issued_webhook_credentials.endpoint_token,
                secret=issued_webhook_credentials.secret,
            )
        return detail

    async def list_triggers(
        self,
        agent_id: UUID,
        *,
        status: AutomationTriggerStatus | None,
        limit: int,
        offset: int,
    ) -> AutomationTriggerListResponse:
        """Return one page of the creator's non-archived agent bindings."""

        if status == AutomationTriggerStatus.ARCHIVED:
            raise AutomationServiceError("Archived triggers are not available from the user list")
        agent = await self._get_agent(agent_id)
        rows, total = await self.access.list_triggers(
            agent_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        return AutomationTriggerListResponse(
            items=[trigger_summary(row, current_target_revision=agent.revision) for row in rows],
            total_count=total,
            limit=limit,
            offset=offset,
        )

    async def get_trigger(self, agent_id: UUID, trigger_id: UUID) -> AutomationTriggerDetail:
        """Return one binding with its compiled matcher and pinned target."""

        agent = await self._get_agent(agent_id)
        trigger = await self.access.get_trigger(agent_id, trigger_id)
        if trigger is None:
            raise AutomationServiceError("Trigger not found", status_code=404)
        return trigger_detail(trigger, current_target_revision=agent.revision)

    async def invoke_manual_trigger(
        self,
        agent_id: UUID,
        trigger_id: UUID,
        body: AutomationManualInvocationRequest,
    ) -> AutomationManualInvocationResponse:
        """Invoke one authorized manual binding with an atomic subject fanout."""

        await self._get_agent(agent_id)
        trigger = await self.access.get_trigger(agent_id, trigger_id)
        if trigger is None:
            raise AutomationServiceError("Trigger not found", status_code=404)
        self._require_manage_authority(trigger)
        items = await ManualInvocationService(
            self.session,
            self.organisation_id,
            self.created_by_user_id,
            is_admin=self.is_admin,
        ).invoke(
            trigger,
            invocation_id=body.invocation_id,
            subjects=tuple(ManualSubject(item.type, item.id) for item in body.subjects),
        )
        return AutomationManualInvocationResponse(
            invocation_id=body.invocation_id,
            correlation_id=cast(str, items[0].envelope.correlation_id),
            items=[
                AutomationManualInvocationSubjectResult(
                    event_id=item.envelope.event_id,
                    subject=body.subjects[index] if body.subjects else None,
                )
                for index, item in enumerate(items)
            ],
        )

    async def replace_trigger(
        self,
        agent_id: UUID,
        trigger_id: UUID,
        body: AutomationTriggerUpdateRequest,
    ) -> AutomationTriggerDetail:
        """Replace decisions while preserving the explicit target revision pin."""

        agent = await self._get_agent(agent_id)
        self._require_activatable(agent.status, body.status)
        current = await self.access.get_trigger(agent_id, trigger_id)
        if current is None:
            raise AutomationServiceError("Trigger not found", status_code=404)
        self._require_manage_authority(current)
        pinned_tools = current.target_snapshot.get("allowed_tools", [])
        pinned_skills = current.target_snapshot.get("allowed_skills", [])
        validated = await self._validate(
            body,
            target_allowed_tools=pinned_tools,
            target_allowed_skills=pinned_skills,
        )
        self._require_personal_target_owner(agent, validated)
        issued_webhook_credentials = None
        if validated.definition.family.value == "webhook":
            if self._enum_value(current.family) == "webhook" and webhook_config_is_provisioned(
                current.trigger_config
            ):
                validated = replace(
                    validated,
                    trigger_config=retain_webhook_credentials(
                        validated.trigger_config,
                        current.trigger_config,
                    ),
                )
            else:
                internal_config, issued_webhook_credentials = provision_webhook_config(
                    validated.trigger_config,
                    organisation_id=self.organisation_id,
                    root_secret=await self._webhook_root_secret(),
                    now=self.webhook_clock(),
                )
                validated = replace(validated, trigger_config=internal_config)
        config_hash = self._config_hash(
            target_id=agent_id,
            target_revision=current.target_revision,
            validated=validated,
        )
        if await self.access.find_duplicate(
            agent_id,
            config_hash,
            exclude_trigger_id=trigger_id,
        ):
            raise AutomationServiceError("An equivalent trigger already exists", status_code=409)
        updated = await self.access.replace_trigger(
            agent_id,
            trigger_id,
            expected_updated_at=body.expected_updated_at,
            values={
                "name": body.name,
                "status": body.status.value,
                "trigger_key": validated.definition.trigger_key,
                "family": validated.definition.family.value,
                "match_event_keys": list(validated.definition.match_event_keys),
                "trigger_config": validated.trigger_config,
                "watch_config": validated.watch_config,
                "filter_config": validated.filter_config,
                "execution_principal_type": validated.execution_principal_type.value,
                "execution_principal_id": validated.execution_principal_id,
                "scope_type": validated.scope_type.value,
                "scope_config": validated.scope_config,
                "audience_mode": validated.audience_mode.value,
                "audience_config": validated.audience_config,
                "task_config": validated.task_config,
                "delivery_config": validated.delivery_config,
                "next_trigger_at": validated.next_trigger_at,
                "config_hash": config_hash,
            },
        )
        if updated is None:
            await self._raise_conditional_failure(agent_id, trigger_id, body.expected_updated_at)
        detail = trigger_detail(updated, current_target_revision=agent.revision)
        if issued_webhook_credentials is not None:
            detail.webhook_credentials = AutomationWebhookCredentials(
                endpoint_token=issued_webhook_credentials.endpoint_token,
                secret=issued_webhook_credentials.secret,
            )
        return detail

    async def rotate_webhook_secret(
        self,
        agent_id: UUID,
        trigger_id: UUID,
        body: AutomationWebhookRotateRequest,
    ) -> AutomationWebhookCredentials:
        """Rotate one webhook secret while retaining a bounded verification grace."""

        await self._get_agent(agent_id)
        current = await self.access.get_trigger(agent_id, trigger_id)
        if current is None:
            raise AutomationServiceError("Trigger not found", status_code=404)
        self._require_manage_authority(current)
        if self._enum_value(current.family) != "webhook":
            raise AutomationServiceError("Only webhook triggers have signing secrets")
        try:
            trigger_config, secret = rotate_webhook_secret(
                current.trigger_config,
                organisation_id=self.organisation_id,
                root_secret=await self._webhook_root_secret(),
                now=self.webhook_clock(),
            )
        except ValueError as exc:
            raise AutomationServiceError(str(exc), status_code=409) from exc
        config_hash = self._stored_config_hash(
            current,
            target_revision=current.target_revision,
            trigger_config=trigger_config,
        )
        if await self.access.find_duplicate(
            agent_id,
            config_hash,
            exclude_trigger_id=trigger_id,
        ):
            raise AutomationServiceError("An equivalent trigger already exists", status_code=409)
        updated = await self.access.replace_trigger(
            agent_id,
            trigger_id,
            expected_updated_at=body.expected_updated_at,
            values={"trigger_config": trigger_config, "config_hash": config_hash},
        )
        if updated is None:
            await self._raise_conditional_failure(
                agent_id,
                trigger_id,
                body.expected_updated_at,
            )
        return AutomationWebhookCredentials(secret=secret)

    async def refresh_target(
        self,
        agent_id: UUID,
        trigger_id: UUID,
        *,
        expected_updated_at: datetime,
    ) -> AutomationTriggerDetail:
        """Explicitly repin one binding to the agent's current executable revision."""

        agent = await self._get_agent(agent_id)
        current = await self.access.get_trigger(agent_id, trigger_id)
        if current is None:
            raise AutomationServiceError("Trigger not found", status_code=404)
        self._require_manage_authority(current)
        self._require_activatable(agent.status, current.status)
        blocked_tools = set(agent.allowed_tools) & RETRY_UNSAFE_AUTOMATION_TOOLS
        if blocked_tools:
            raise AutomationServiceError(
                "Automation mutating tools are disabled until actions are retry-safe",
            )
        if self._enum_value(
            current.execution_principal_type
        ) == AutomationPrincipalType.ORGANISATION_SERVICE.value and (
            agent.allowed_tools or agent.allowed_skills
        ):
            raise AutomationServiceError(
                "Organisation service agents cannot use tools or skills",
            )
        config_hash = self._stored_config_hash(current, target_revision=agent.revision)
        if await self.access.find_duplicate(
            agent_id,
            config_hash,
            exclude_trigger_id=trigger_id,
        ):
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
        self,
        agent_id: UUID,
        trigger_id: UUID,
        *,
        expected_updated_at: datetime,
    ) -> None:
        """Archive one binding while retaining run and audit references."""

        await self._get_agent(agent_id)
        current = await self.access.get_trigger(agent_id, trigger_id)
        if current is None:
            raise AutomationServiceError("Trigger not found", status_code=404)
        self._require_manage_authority(current)
        archived = await self.access.archive_trigger(
            agent_id,
            trigger_id,
            expected_updated_at=expected_updated_at,
        )
        if archived is None:
            await self._raise_conditional_failure(agent_id, trigger_id, expected_updated_at)

    async def _validate(
        self,
        body: AutomationTriggerCreateRequest | AutomationTriggerUpdateRequest,
        *,
        target_allowed_tools: Any,
        target_allowed_skills: Any,
    ) -> ValidatedTriggerDefinition:
        try:
            self.validator.registry = (await self.registry_service.snapshot()).registry
            return await self.validator.validate(
                body,
                target_allowed_tools=target_allowed_tools,
                target_allowed_skills=target_allowed_skills,
            )
        except ValueError as exc:
            raise AutomationServiceError(str(exc)) from exc

    async def _webhook_root_secret(self) -> str:
        try:
            secret = await self.webhook_root_secret_provider()
        except Exception as exc:
            raise AutomationServiceError(
                "Webhook credential service is unavailable",
                status_code=503,
            ) from exc
        if not secret:
            raise AutomationServiceError(
                "Webhook credential service is unavailable",
                status_code=503,
            )
        return secret

    async def _get_agent(self, agent_id: UUID) -> Any:
        agent = await self.access.get_agent(agent_id)
        if agent is None:
            raise AutomationServiceError("Agent not found", status_code=404)
        return agent

    def _require_manage_authority(self, trigger: AutomationTrigger) -> None:
        if (
            self._enum_value(trigger.execution_principal_type)
            == AutomationPrincipalType.ORGANISATION_SERVICE.value
            and not self.is_admin
        ):
            raise AutomationServiceError(
                "Organisation bindings require an organisation admin",
                status_code=403,
            )

    def _require_personal_target_owner(
        self,
        agent: Any,
        validated: ValidatedTriggerDefinition,
    ) -> None:
        if (
            validated.execution_principal_type == AutomationPrincipalType.USER
            and agent.owner_user_id != self.created_by_user_id
        ):
            raise AutomationServiceError(
                "Personal bindings require an agent owned by the current user",
                status_code=403,
            )

    @staticmethod
    def _require_activatable(
        agent_status: AgentStatus,
        trigger_status: AutomationTriggerStatus,
    ) -> None:
        if trigger_status == AutomationTriggerStatus.ACTIVE and agent_status != AgentStatus.ACTIVE:
            raise AutomationServiceError(
                "An active trigger requires an active agent",
                status_code=409,
            )

    async def _raise_conditional_failure(
        self,
        agent_id: UUID,
        trigger_id: UUID,
        expected_updated_at: datetime,
    ) -> NoReturn:
        current = await self.access.get_trigger(agent_id, trigger_id, include_archived=True)
        if current is None or current.status == AutomationTriggerStatus.ARCHIVED:
            raise AutomationServiceError("Trigger not found", status_code=404)
        if current.updated_at != expected_updated_at:
            raise AutomationServiceError(
                "Trigger changed since it was loaded; refresh and retry",
                status_code=409,
            )
        raise AutomationServiceError("Trigger could not be updated", status_code=409)

    @classmethod
    def _config_hash(
        cls,
        *,
        target_id: UUID,
        target_revision: int,
        validated: ValidatedTriggerDefinition,
    ) -> str:
        return cls._hash_payload(
            cls._run_affecting_payload(
                target_id=target_id,
                target_revision=target_revision,
                trigger_key=validated.definition.trigger_key,
                family=validated.definition.family.value,
                match_event_keys=list(validated.definition.match_event_keys),
                trigger_config=validated.trigger_config,
                watch_config=validated.watch_config,
                filter_config=validated.filter_config,
                execution_principal_type=validated.execution_principal_type.value,
                execution_principal_id=validated.execution_principal_id,
                scope_type=validated.scope_type.value,
                scope_config=validated.scope_config,
                audience_mode=validated.audience_mode.value,
                audience_config=validated.audience_config,
                task_config=validated.task_config,
                delivery_config=validated.delivery_config,
            )
        )

    @classmethod
    def _stored_config_hash(
        cls,
        trigger: AutomationTrigger,
        *,
        target_revision: int,
        trigger_config: dict[str, Any] | None = None,
    ) -> str:
        return cls._hash_payload(
            cls._run_affecting_payload(
                target_id=trigger.target_id,
                target_revision=target_revision,
                trigger_key=trigger.trigger_key,
                family=cls._enum_value(trigger.family),
                match_event_keys=list(trigger.match_event_keys),
                trigger_config=(
                    trigger.trigger_config if trigger_config is None else trigger_config
                ),
                watch_config=trigger.watch_config,
                filter_config=trigger.filter_config,
                execution_principal_type=cls._enum_value(trigger.execution_principal_type),
                execution_principal_id=trigger.execution_principal_id,
                scope_type=cls._enum_value(trigger.scope_type),
                scope_config=trigger.scope_config,
                audience_mode=cls._enum_value(trigger.audience_mode),
                audience_config=trigger.audience_config,
                task_config=trigger.task_config,
                delivery_config=trigger.delivery_config,
            )
        )

    @classmethod
    def _run_affecting_payload(cls, **value: Any) -> dict[str, Any]:
        watch = value.get("watch_config")
        if watch:
            watch = {**watch, "paths": sorted(watch["paths"])}
        trigger_config = value["trigger_config"]
        if value["family"] == "webhook":
            trigger_config = public_webhook_config(trigger_config)
        return {
            "target": {
                "type": AutomationTargetType.AGENT.value,
                "id": str(value["target_id"]),
                "revision": value["target_revision"],
            },
            "trigger": {
                "key": value["trigger_key"],
                "family": value["family"],
                "event_keys": value["match_event_keys"],
                "config": trigger_config,
                "watch": watch,
                "filter": cls._canonical_filter(value["filter_config"]),
            },
            "authority": {
                "principal_type": value["execution_principal_type"],
                "principal_id": str(value["execution_principal_id"]),
                "scope_type": value["scope_type"],
                "scope": value["scope_config"],
                "audience_mode": value["audience_mode"],
                "audience": value["audience_config"],
            },
            "task": value["task_config"],
            "delivery": value["delivery_config"],
        }

    @classmethod
    def _canonical_filter(cls, value: Any) -> Any:
        """Canonicalize only commutative matcher groups and set-valued operators."""

        if not isinstance(value, dict):
            return value
        for mode in ("all", "any"):
            if set(value) == {mode} and isinstance(value[mode], list):
                children = [cls._canonical_filter(child) for child in value[mode]]
                return {
                    mode: sorted(
                        children,
                        key=lambda child: json.dumps(
                            child,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    )
                }
        result = dict(value)
        if result.get("operator") in {
            "in",
            "not_in",
            "contains_any",
            "contains_all",
        } and isinstance(result.get("value"), list):
            result["value"] = sorted(
                result["value"],
                key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
            )
        return result

    @staticmethod
    def _enum_value(value: Any) -> Any:
        return getattr(value, "value", value)

    @staticmethod
    def _hash_payload(value: dict[str, Any]) -> str:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
