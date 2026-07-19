"""Tool-free contact research summary agent.

Called after provider enrichment has already been persisted. This agent only
turns selected provider evidence into concise UI copy; it has no tools and does not
own any source-of-truth contact fields.
"""

from __future__ import annotations

import json
from typing import Any, Literal
from uuid import UUID

from agno.agent import Agent
from agno.models.azure.openai_chat import AzureOpenAI
from agno.models.message import Message
from pydantic import BaseModel, Field, ValidationError

from src.agents.common.token_tracking import TokenUsageData, extract_token_usage
from src.agents.workflows.contact_research_summary.prompts import (
    get_contact_research_summary_system_prompt,
)
from src.infra.config.agent_config import agent_config
from src.infra.utils.logger import get_logger
from src.service.crm.contacts.research_service import (
    MAX_CONTACT_RESEARCH_CHARS,
    ContactResearchSummaryResult,
    build_contact_research_fact_pack,
)
from src.service.orgnisation.kai_training_service import append_organisation_profile

logger = get_logger(__name__)


class _SummaryOutput(BaseModel):
    status: Literal["generated", "skipped", "error"] = Field(
        ...,
        description="generated, skipped, or error",
    )
    error_detail: str | None = None
    summary_markdown: str | None = Field(default=None, max_length=MAX_CONTACT_RESEARCH_CHARS)


class ContactResearchSummaryAgent:
    """Generate a career-complete contact profile from persisted provider evidence."""

    def __init__(
        self,
        organisation_id: UUID,
        org_name: str | None = None,
        organisation_profile: str | None = None,
    ):
        self.organisation_id = organisation_id
        self.org_name = org_name or "Our Organization"
        self.last_token_usage: TokenUsageData | None = None

        config = agent_config.get_config("contact_research_summary_agent")
        self.agent = Agent(
            model=AzureOpenAI(
                id=config.model,
                api_version=config.api_version,
                retries=config.retries,
                delay_between_retries=1,
                exponential_backoff=True,
            ),
            description="Contact Research Summary Agent",
            tools=[],
            tool_call_limit=0,
            output_schema=_SummaryOutput,
            debug_mode=config.debug_mode,
            system_message=append_organisation_profile(
                get_contact_research_summary_system_prompt(self.org_name),
                organisation_profile,
            ),
            telemetry=False,
        )

    async def summarize(
        self,
        *,
        contact_id: UUID,
        profile_enrichment: dict[str, Any],
    ) -> ContactResearchSummaryResult:
        """Summarize selected provider evidence for one contact without calling tools."""
        if not profile_enrichment:
            return ContactResearchSummaryResult(status="skipped")

        fact_pack = build_contact_research_fact_pack(profile_enrichment)
        response = await self.agent.arun(
            [
                Message(
                    role="user",
                    content=(
                        "Create the contact profile from this provider evidence only.\n\n"
                        f"Contact ID: {contact_id}\n"
                        f"Provider evidence:\n{json.dumps(fact_pack, default=str)}"
                    ),
                )
            ]
        )
        self.last_token_usage = extract_token_usage(
            response,
            agent_name="ContactResearchSummaryAgent",
        )
        return self._normalize_output(response.content)

    @staticmethod
    def _normalize_output(content: object) -> ContactResearchSummaryResult:
        if isinstance(content, ContactResearchSummaryResult):
            return content
        if isinstance(content, _SummaryOutput):
            return ContactResearchSummaryResult(
                status=content.status,
                error_detail=content.error_detail,
                summary_markdown=content.summary_markdown,
            )
        if isinstance(content, dict):
            try:
                parsed = _SummaryOutput.model_validate(content)
                return ContactResearchSummaryResult(
                    status=parsed.status,
                    error_detail=parsed.error_detail,
                    summary_markdown=parsed.summary_markdown,
                )
            except ValidationError:
                logger.warning("Contact summary dict failed validation: %.200s", str(content))
        if isinstance(content, str):
            try:
                parsed = _SummaryOutput.model_validate_json(content.strip())
                return ContactResearchSummaryResult(
                    status=parsed.status,
                    error_detail=parsed.error_detail,
                    summary_markdown=parsed.summary_markdown,
                )
            except (ValidationError, json.JSONDecodeError):
                if content.strip():
                    if len(content.strip()) > MAX_CONTACT_RESEARCH_CHARS:
                        return ContactResearchSummaryResult(
                            status="error",
                            error_detail="Summary agent exceeded the output size limit",
                        )
                    return ContactResearchSummaryResult(
                        status="generated",
                        summary_markdown=content.strip(),
                    )
        return ContactResearchSummaryResult(
            status="error",
            error_detail="Summary agent returned unstructured response",
        )
