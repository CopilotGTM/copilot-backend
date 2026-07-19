"""Meeting-level summary agent for meetings without deal context."""

from __future__ import annotations

import json
import re
from uuid import UUID

from agno.agent import Agent
from agno.models.azure.openai_chat import AzureOpenAI
from pydantic import ValidationError

from src.agents.common.token_tracking import TokenUsageData, extract_token_usage
from src.agents.workflows.meeting_level_summary.models import MeetingLevelSummaryInput
from src.agents.workflows.meeting_level_summary.prompts import (
    build_non_deal_meeting_summary_user_prompt,
    get_non_deal_meeting_summary_system_prompt,
)
from src.agents.workflows.summary.models import SummaryData
from src.infra.config.agent_config import agent_config
from src.infra.utils.logger import get_logger
from src.service.orgnisation.kai_training_service import (
    append_organisation_context,
    with_organisation_context_policy,
)

logger = get_logger(__name__)


class MeetingLevelSummaryAgent:
    """Generate transcript-backed summaries for meetings that are not deal-linked.

    This workflow is isolated from the deal-summary package so it cannot inherit
    deal tools, deal prompts, or UI progress behavior by accident.
    """

    def __init__(
        self,
        *,
        organisation_id: UUID,
        org_name: str | None = None,
        organisation_profile: str | None = None,
    ):
        """Initialize the no-deal summary agent."""
        self.organisation_id = organisation_id
        self.org_name = org_name or "Our Organization"
        self.organisation_profile = organisation_profile
        self.last_token_usage: TokenUsageData | None = None

        config = agent_config.get_config("summary_agent")
        self.agent = Agent(
            model=AzureOpenAI(
                id=config.model,
                api_version=config.api_version,
                retries=config.retries,
                delay_between_retries=1,
                exponential_backoff=True,
            ),
            output_schema=SummaryData,
            description=with_organisation_context_policy(
                get_non_deal_meeting_summary_system_prompt(self.org_name)
            ),
            markdown=True,
            debug_mode=config.debug_mode,
            telemetry=False,
        )

    @staticmethod
    def _strip_summary_fences(text: str) -> str:
        """Remove outer markdown fences commonly returned around meeting summaries."""
        stripped = text.strip()
        fenced_match = re.match(r"^```[^\n]*\n(?P<body>.*)\n```$", stripped, re.DOTALL)
        if fenced_match:
            return fenced_match.group("body").strip()
        return stripped

    def _normalize_summary_output(self, content: object) -> SummaryData:
        """Normalize imperfect model output into `SummaryData` when safely possible."""
        if isinstance(content, SummaryData):
            return content

        if isinstance(content, dict):
            try:
                validated = SummaryData.model_validate(content)
                return SummaryData(
                    meeting_summary=self._strip_summary_fences(validated.meeting_summary),
                )
            except ValidationError as e:
                raise ValueError("dict output failed validation") from e

        if isinstance(content, str):
            stripped = content.strip()
            fenced_match = re.match(r"^```[^\n]*\n(?P<body>.*)\n```$", stripped, re.DOTALL)
            if fenced_match:
                stripped = fenced_match.group("body").strip()

            try:
                validated = SummaryData.model_validate_json(stripped)
                return SummaryData(
                    meeting_summary=self._strip_summary_fences(validated.meeting_summary),
                )
            except (ValidationError, json.JSONDecodeError):
                pass

            try:
                payload = json.loads(stripped)
                if isinstance(payload, dict):
                    validated = SummaryData.model_validate(payload)
                    return SummaryData(
                        meeting_summary=self._strip_summary_fences(validated.meeting_summary),
                    )
            except (ValidationError, json.JSONDecodeError, TypeError):
                pass

            return SummaryData(meeting_summary=self._strip_summary_fences(stripped))

        raise ValueError("unsupported summary output type")

    async def generate(self, input_data: MeetingLevelSummaryInput) -> SummaryData:
        """Generate a meeting-only summary from supplied transcript and metadata."""
        logger.info(
            "MeetingLevelSummaryAgent generating summary for meeting %s",
            input_data.event_id,
        )

        response = await self.agent.arun(
            append_organisation_context(
                build_non_deal_meeting_summary_user_prompt(input_data),
                self.organisation_profile,
            )
        )
        self.last_token_usage = extract_token_usage(
            response,
            agent_name="MeetingLevelSummaryAgent",
        )

        try:
            return self._normalize_summary_output(response.content)
        except ValueError:
            logger.warning(
                "MeetingLevelSummaryAgent returned non-structured response for meeting %s: %.200s",
                input_data.event_id,
                str(response.content),
            )
            return SummaryData(meeting_summary="")
