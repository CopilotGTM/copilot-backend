from pydantic import BaseModel, Field
from typing import List, Dict, AsyncIterator, Iterator
from textwrap import dedent
import json
from agno.workflow import Workflow, RunResponse, RunEvent
from agno.agent import Agent
from agno.models.azure import AzureOpenAI
from agno.tools.duckduckgo import DuckDuckGoTools
from agno.tools.wikipedia import WikipediaTools
import asyncio

from agno.knowledge.pdf import PDFKnowledgeBase
from agno.vectordb.pgvector import PgVector
from agno.document.chunking.recursive import RecursiveChunking
from agno.embedder.azure_openai import AzureOpenAIEmbedder
from agno.models.azure import AzureAIFoundry

from dotenv import load_dotenv
from app_layer.utils.logger import get_logger
from workflows.utils.get_master_db_url import get_master_db_url

from workflows.generate_slide_content.prompts.solution_narration import TECHNICAL_WIN_STRATEGY_PROMPT
from workflows.generate_slide_content.prompts.slides_outline import SLIDE_OUTLINE_PROMPT
from workflows.generate_slide_content.prompts.slide_content import SLIDE_CONTENT_PROMPT

load_dotenv()
logger = get_logger()


class SlideItem(BaseModel):
    """The slide item model."""

    slide_number: int = Field(..., description="The number of the slide")
    slide_title: str = Field(..., description="The title of the slide")
    description: str = Field(..., description="Why this slide is important")


class SlideOutline(BaseModel):
    """The slide outline model."""

    slide_outline: List[SlideItem]


class SolutionNarration(BaseModel):
    """The slide item model."""

    solutioning_narration: str = Field(..., description="The number of the slide")


class SlideContentGenerator(Workflow):

    def __init__(self, provider: str = "azure", model_name: str = "o4-mini", **kwargs):
        super().__init__(**kwargs)
        self.df_url = get_master_db_url()
        self.knowledge_base = PDFKnowledgeBase(
            path="",
            vector_db=PgVector(
                table_name="xeno_knowledge",
                db_url=self.df_url,
                embedder=AzureOpenAIEmbedder(),
            ),
            chunking_strategy=RecursiveChunking(),
        )

        self.story_narration_outline = Agent(
            model=AzureOpenAI(id="o4-mini", api_version="2025-04-01-preview", reasoning_effort="high"),
            description=TECHNICAL_WIN_STRATEGY_PROMPT,
            knowledge=self.knowledge_base,
            debug_mode=False,
            markdown=True,
        )

        self.generate_outline = Agent(
            model=AzureOpenAI(
                id="o4-mini",
                api_version="2024-12-01-preview",
            ),
            description=SLIDE_OUTLINE_PROMPT,
            knowledge=self.knowledge_base,
            response_model=SlideOutline,
            reasoning=True,
            structured_outputs=True,
            debug_mode=False,
            markdown=True,
        )

        self.generate_slide_content = Agent(
            model=AzureOpenAI(
                id="o4-mini",
                api_version="2024-12-01-preview",
            ),
            description=SLIDE_CONTENT_PROMPT,
            knowledge=self.knowledge_base,
            markdown=True,
        )

    async def run_workflow(self, input_summary: str) -> AsyncIterator[RunResponse]:

        # 1) Planning
        logger.info("working on the planning phase")
        logger.info("Generating Solutioning Narration")
        story_narration = await self.story_narration_outline.arun(input_summary)
        story_narration_outline = story_narration.content
        logger.info(f"Solutioning Narration Generated")

        # 2) Slide Outline Generation
        logger.info("Generating Slide Outline")
        slide_outline = await self.generate_outline.arun(
            f"""
                Summary of the meeting: 
                {input_summary}
                -----------------------------------------------------
                -----------------------------------------------------
                Solutioning Narration Suggested by Senior Sales Engineer: 
                {story_narration_outline}
            """
        )
        logger.info(f"Slide Outline Generated")

        previous_slides: List[Dict] = []

        logger.info("Generating Slide Content")
        for slide in slide_outline.content.slide_outline:
            logger.info(f"Slide Number: {slide.slide_number}, Slide Title: {slide.slide_title}")

            # Add previous slides to the prompt
            if previous_slides:
                previous_slides_str = "\n".join(
                    [
                        f"Slide Number: {s['slide_number']}, Slide Title: {s['slide_title']}, Slide Content: {s['slide_content']}"
                        for s in previous_slides
                    ]
                )
            else:
                previous_slides_str = ""
            slide_content = await self.generate_slide_content.arun(
                f"""
                    Slide Number: {slide.slide_number},
                    -----------------------------------------------------
                    Proposed Slide Title: {slide.slide_title},
                    -----------------------------------------------------
                    Description: {slide.description},
                    -----------------------------------------------------
                    Meeting Summary: {input_summary}
                    -----------------------------------------------------
                    Solutioning Narration Suggested by Senior Sales Engineer: 
                    {story_narration_outline}
                    -----------------------------------------------------
                    Data of Earlier Slides Created:
                    {previous_slides_str}
                """
            )

            output_format = {
                "slide_number": slide.slide_number,
                "slide_title": slide.slide_title,
                "slide_content": slide_content.content,
            }

            # Append to our history before yielding
            previous_slides.append(output_format)

            yield RunResponse(
                event=RunEvent.run_response,
                content=output_format,
            )
        logger.info(f"Slide Content Generated")


# async def main():

#     transcript_summary_agent = SlideContentGenerator()
#     logger.info("Workflow execution started.")
#     async for response in transcript_summary_agent.run_workflow(input_summary):
#         logger.info(response.content)  # Prints each chunk directly


# if __name__ == "__main__":
#     asyncio.run(main())
