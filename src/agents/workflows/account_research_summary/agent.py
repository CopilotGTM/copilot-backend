"""Tool-free account research summary agent."""

from __future__ import annotations

import json
from typing import Any, Literal
from uuid import UUID

from agno.agent import Agent
from agno.models.azure.openai_chat import AzureOpenAI
from agno.models.message import Message
from pydantic import BaseModel, Field, ValidationError

from src.agents.common.token_tracking import TokenUsageData, extract_token_usage
from src.agents.workflows.account_research_summary.prompts import (
    get_account_research_summary_system_prompt,
)
from src.infra.config.agent_config import agent_config
from src.infra.utils.logger import get_logger
from src.service.crm.accounts.research_service import AccountResearchSummaryResult
from src.service.orgnisation.kai_training_service import append_organisation_profile

logger = get_logger(__name__)


class _SummaryOutput(BaseModel):
    status: Literal["generated", "skipped", "error"] = Field(
        ...,
        description="generated, skipped, or error",
    )
    error_detail: str | None = None
    summary_markdown: str | None = None


class AccountResearchSummaryAgent:
    """Generate a short account UI summary from already-persisted provider data."""

    def __init__(
        self,
        organisation_id: UUID,
        org_name: str | None = None,
        organisation_profile: str | None = None,
    ):
        self.organisation_id = organisation_id
        self.org_name = org_name or "Our Organization"
        self.last_token_usage: TokenUsageData | None = None

        config = agent_config.get_config("account_research_summary_agent")
        self.agent = Agent(
            model=AzureOpenAI(
                id=config.model,
                api_version=config.api_version,
                retries=config.retries,
                delay_between_retries=1,
                exponential_backoff=True,
            ),
            description="Account Research Summary Agent",
            tools=[],
            tool_call_limit=0,
            output_schema=_SummaryOutput,
            debug_mode=config.debug_mode,
            system_message=append_organisation_profile(
                get_account_research_summary_system_prompt(self.org_name),
                organisation_profile,
            ),
            telemetry=False,
        )

    async def summarize(
        self,
        *,
        company_id: UUID,
        company_enrichment: dict[str, Any],
    ) -> AccountResearchSummaryResult:
        """Summarize provider data for one account without calling tools."""
        if not company_enrichment:
            return AccountResearchSummaryResult(status="skipped")

        response = await self.agent.arun(
            [
                Message(
                    role="user",
                    content=(
                        "Write the account summary from this provider data only.\n\n"
                        f"Company ID: {company_id}\n"
                        f"Provider data:\n{json.dumps(company_enrichment, default=str)}"
                    ),
                )
            ]
        )
        self.last_token_usage = extract_token_usage(
            response,
            agent_name="AccountResearchSummaryAgent",
        )
        return self._normalize_output(response.content)

    @staticmethod
    def _normalize_output(content: object) -> AccountResearchSummaryResult:
        if isinstance(content, AccountResearchSummaryResult):
            return content
        if isinstance(content, _SummaryOutput):
            return AccountResearchSummaryResult(
                status=content.status,
                error_detail=content.error_detail,
                summary_markdown=content.summary_markdown,
            )
        if isinstance(content, dict):
            try:
                parsed = _SummaryOutput.model_validate(content)
                return AccountResearchSummaryResult(
                    status=parsed.status,
                    error_detail=parsed.error_detail,
                    summary_markdown=parsed.summary_markdown,
                )
            except ValidationError:
                logger.warning("Account summary dict failed validation: %.200s", str(content))
        if isinstance(content, str):
            try:
                parsed = _SummaryOutput.model_validate_json(content.strip())
                return AccountResearchSummaryResult(
                    status=parsed.status,
                    error_detail=parsed.error_detail,
                    summary_markdown=parsed.summary_markdown,
                )
            except (ValidationError, json.JSONDecodeError):
                if content.strip():
                    return AccountResearchSummaryResult(
                        status="generated",
                        summary_markdown=content.strip(),
                    )
        return AccountResearchSummaryResult(
            status="error",
            error_detail="Summary agent returned unstructured response",
        )
