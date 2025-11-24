from __future__ import annotations

from uuid import UUID

from agno.agent import Agent
from agno.models.azure import AzureOpenAI
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.calendar import (
    CalendarEvent,
    CalendarEventDealAssociation,
    GenerationStatus,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.crm.stakeholders import DealStakeholder
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class BriefingData(BaseModel):
    objective: str = Field(..., description="The primary goal of this meeting.")
    summary: str = Field(..., description="A concise summary of the deal context.")
    stakeholder_analysis: str = Field(..., description="Key insights about the attendees.")
    pain_points: list[str] = Field(..., description="Known pain points to address.")
    suggested_questions: list[str] = Field(..., description="Strategic questions to ask.")
    potential_objections: list[str] = Field(
        ..., description="Likely objections and how to handle them."
    )
    checklist: list[str] = Field(..., description="Pre-meeting preparation checklist.")


class BriefingGeneratorService:
    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        # Initialize the AI Agent
        self.agent = Agent(
            model=AzureOpenAI(id="gpt-5-mini", api_version="2024-12-01-preview"),
            response_model=BriefingData,
            description="You are an expert Sales Engineer assistant. Your goal is to prepare a comprehensive meeting briefing for a sales representative.",
            instructions=[
                "Analyze the provided Deal Context, Stakeholder Information, and Meeting Details.",
                "Generate a strategic briefing that helps the rep close the deal.",
                "Focus on actionable insights, not generic advice.",
                "If specific information is missing, make reasonable inferences based on the deal stage and standard sales practices.",
            ],
        )

    async def generate_briefing(self, deal_id: UUID, event_id: UUID) -> BriefingData | None:
        """
        Generates a meeting briefing for a specific deal and event.
        Updates the association record with the result.
        """
        logger.info(f"Generating briefing for Deal {deal_id} and Event {event_id}")

        # 1. Fetch Context
        deal = await self._get_deal(deal_id)
        event = await self._get_event(event_id)
        stakeholders = await self._get_stakeholders(deal_id)
        association = await self._get_association(deal_id, event_id)

        if not deal or not event or not association:
            logger.error("Missing required data for briefing generation.")
            return None

        # Update status to GENERATING
        association.meeting_prep_status = GenerationStatus.GENERATING
        await self.session.commit()

        try:
            # 2. Construct Prompt Context
            context_str = self._build_context_string(deal, event, stakeholders)

            # 3. Call AI
            response = await self.agent.arun(context_str)
            briefing_data: BriefingData = response.content

            # 4. Save Result
            association.meeting_prep_data = briefing_data.model_dump()
            association.meeting_prep_status = GenerationStatus.COMPLETED
            await self.session.commit()

            logger.info("Briefing generated successfully.")
            return briefing_data

        except Exception as e:
            logger.error(f"Failed to generate briefing: {e}")
            association.meeting_prep_status = GenerationStatus.FAILED
            await self.session.commit()
            return None

    async def _get_deal(self, deal_id: UUID) -> CRMDeal | None:
        stmt = select(CRMDeal).where(
            col(CRMDeal.deal_id) == deal_id,
            col(CRMDeal.organisation_id) == self.organisation_id,
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def _get_event(self, event_id: UUID) -> CalendarEvent | None:
        stmt = select(CalendarEvent).where(
            col(CalendarEvent.event_id) == event_id,
            col(CalendarEvent.organisation_id) == self.organisation_id,
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def _get_stakeholders(self, deal_id: UUID) -> list[DealStakeholder]:
        stmt = select(DealStakeholder).where(
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def _get_association(
        self, deal_id: UUID, event_id: UUID
    ) -> CalendarEventDealAssociation | None:
        stmt = select(CalendarEventDealAssociation).where(
            col(CalendarEventDealAssociation.deal_id) == deal_id,
            col(CalendarEventDealAssociation.event_id) == event_id,
            col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    def _build_context_string(
        self, deal: CRMDeal, event: CalendarEvent, stakeholders: list[DealStakeholder]
    ) -> str:
        stakeholder_info = "\n".join(
            [
                f"- {s.name} ({s.job_title}): {s.stance} (Temp: {s.temperature})"
                for s in stakeholders
            ]
        )

        return f"""
        Meeting Context:
        - Event: {event.data_payload.get("subject", "Unknown Subject") if event.data_payload else "Unknown"}
        - Date: {event.start_at}
        - Description: {event.data_payload.get("description", "") if event.data_payload else ""}

        Deal Context:
        - Name: {deal.name}
        - Stage: {deal.stage_id} (ID)
        - Amount: {deal.amount}
        - Close Date: {deal.close_date}

        Stakeholders:
        {stakeholder_info}
        """
