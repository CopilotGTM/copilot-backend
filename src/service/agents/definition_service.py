"""Application service for private user-owned agent definitions.

FastAPI handlers and future internal callers construct this service with immutable
organisation and owner identifiers. The service validates complete definitions, delegates all
SQL to the owner-scoped access layer, and flushes through that layer without committing.
"""

from __future__ import annotations

from typing import NoReturn
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.api.schemas.agent_models import (
    AgentCapabilitiesUpdateRequest,
    AgentCreateRequest,
    AgentDetail,
    AgentListResponse,
    AgentUpdateRequest,
)
from src.domain.models.organisation_tables import (
    AgentSourceType,
    AgentStatus,
    AgentVisibility,
    UserAgent,
)
from src.infra.utils.logger import get_logger
from src.service.agents.access import AgentDefinitionAccess
from src.service.agents.errors import AgentServiceError
from src.service.agents.serializers import agent_detail, agent_summary
from src.service.agents.template_metadata import (
    agent_definition_from_template,
    get_agent_template,
)
from src.service.agents.validator import AgentDefinitionValidator

logger = get_logger("agent_definition_service")


class AgentDefinitionService:
    """Manage one authenticated user's private executable agent definitions."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        owner_user_id: UUID,
        *,
        is_admin: bool = False,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.owner_user_id = owner_user_id
        self.is_admin = is_admin
        self.access = AgentDefinitionAccess(session, organisation_id, owner_user_id)
        self.validator = AgentDefinitionValidator()

    async def close(self) -> None:
        """Release the request-scoped database session after the handler completes."""

        try:
            await self.session.close()
        except Exception:
            logger.debug("AgentDefinitionService close failed", exc_info=True)

    async def create_agent(self, body: AgentCreateRequest) -> AgentDetail:
        """Create a private user-authored definition under authenticated ownership."""

        return await self._create_agent(
            body,
            source_type=AgentSourceType.USER,
            source_agent_id=None,
            source_version=None,
            agent_type="general",
        )

    async def use_template(self, template_id: UUID, *, version: int) -> AgentDetail:
        """Copy one exact code-owned template into the authenticated user's agents."""

        template = get_agent_template(template_id, version=version)
        if template is None:
            raise AgentServiceError("Agent template not found", status_code=404)
        return await self._create_agent(
            agent_definition_from_template(template),
            source_type=AgentSourceType.SYSTEM,
            source_agent_id=template.template_id,
            source_version=template.version,
            agent_type=template.agent_type,
        )

    async def _create_agent(
        self,
        body: AgentCreateRequest,
        *,
        source_type: AgentSourceType,
        source_agent_id: UUID | None,
        source_version: int | None,
        agent_type: str,
    ) -> AgentDetail:
        """Validate and persist one definition with server-controlled provenance."""

        self.validator.validate_definition(body)
        agent = UserAgent(
            organisation_id=self.organisation_id,
            owner_user_id=self.owner_user_id,
            source_type=source_type,
            source_agent_id=source_agent_id,
            source_version=source_version,
            revision=1,
            visibility=AgentVisibility.PRIVATE,
            status=AgentStatus.ACTIVE,
            agent_type=agent_type,
            name=body.name,
            description=body.description,
            prompt=body.prompt,
            model_name=body.model_name,
            model_settings=body.agent_model_config,
            allowed_tools=body.allowed_tools,
            allowed_skills=body.allowed_skills,
            max_turns=body.max_turns,
            input_schema=body.input_schema,
            output_mode=body.output_mode,
            output_schema=body.output_schema,
        )
        await self.access.create_agent(agent)
        return agent_detail(agent, is_admin=self.is_admin)

    async def list_agents(
        self,
        *,
        status: AgentStatus | None,
        limit: int,
        offset: int,
    ) -> AgentListResponse:
        """Return an offset page of the current owner's non-archived definitions."""

        if not 1 <= limit <= 100:
            raise AgentServiceError("limit must be between 1 and 100")
        if offset < 0:
            raise AgentServiceError("offset must be >= 0")
        if status == AgentStatus.ARCHIVED:
            raise AgentServiceError("Archived agents are not available from the user list")

        rows, total_count = await self.access.list_agents(
            status=status,
            limit=limit,
            offset=offset,
        )
        return AgentListResponse(
            items=[agent_summary(row) for row in rows],
            total_count=total_count,
            limit=limit,
            offset=offset,
        )

    async def get_agent(self, agent_id: UUID) -> AgentDetail:
        """Return one owned definition without revealing foreign or archived identifiers."""

        agent = await self.access.get_agent(agent_id)
        if agent is None:
            raise AgentServiceError("Agent not found", status_code=404)
        return agent_detail(agent, is_admin=self.is_admin)

    async def replace_agent(
        self,
        agent_id: UUID,
        body: AgentUpdateRequest,
    ) -> AgentDetail:
        """Replace mutable fields if the caller's expected revision is still current."""

        self.validator.validate_definition(body)
        updated = await self.access.replace_agent(
            agent_id,
            expected_revision=body.expected_revision,
            values={
                "name": body.name,
                "description": body.description,
                "prompt": body.prompt,
                "model_name": body.model_name,
                "model_config": body.agent_model_config,
                "allowed_tools": body.allowed_tools,
                "allowed_skills": body.allowed_skills,
                "max_turns": body.max_turns,
                "input_schema": body.input_schema,
                "output_mode": body.output_mode.value,
                "output_schema": body.output_schema,
                "status": body.status.value,
            },
        )
        if updated is None:
            await self._raise_conditional_write_failure(agent_id, body.expected_revision)
        return agent_detail(updated, is_admin=self.is_admin)

    async def replace_capabilities(
        self,
        agent_id: UUID,
        body: AgentCapabilitiesUpdateRequest,
    ) -> AgentDetail:
        """Replace only selected tools and skills under optimistic concurrency."""

        self.validator.validate_capabilities(body.allowed_tools, body.allowed_skills)
        updated = await self.access.replace_agent(
            agent_id,
            expected_revision=body.expected_revision,
            values={
                "allowed_tools": body.allowed_tools,
                "allowed_skills": body.allowed_skills,
            },
        )
        if updated is None:
            await self._raise_conditional_write_failure(agent_id, body.expected_revision)
        return agent_detail(updated, is_admin=self.is_admin)

    async def archive_agent(self, agent_id: UUID, *, expected_revision: int) -> None:
        """Archive one owned definition while preserving trigger and run references."""

        archived = await self.access.archive_agent(
            agent_id,
            expected_revision=expected_revision,
        )
        if archived is None:
            await self._raise_conditional_write_failure(agent_id, expected_revision)

    async def _raise_conditional_write_failure(
        self,
        agent_id: UUID,
        expected_revision: int,
    ) -> NoReturn:
        # Re-read through the same owner boundary. Foreign user/org ids remain indistinguishable
        # from absent ids, while an owned newer revision gets an actionable conflict response.
        current = await self.access.get_agent(agent_id, include_archived=True)
        if current is None or current.status == AgentStatus.ARCHIVED:
            raise AgentServiceError("Agent not found", status_code=404)
        if current.revision != expected_revision:
            raise AgentServiceError(
                "Agent changed since it was loaded; refresh and retry",
                status_code=409,
            )
        raise AgentServiceError("Agent could not be updated", status_code=409)
