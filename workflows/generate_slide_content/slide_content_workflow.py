from pydantic import BaseModel, Field
from typing import List, Dict, AsyncIterator, Iterator
from textwrap import dedent
import json
from agno.workflow import Workflow, RunResponse, RunEvent
from agno.agent import Agent
from agno.models.azure import AzureOpenAI
from agno.tools.duckduckgo import DuckDuckGoTools
from agno.tools.wikipedia import WikipediaTools

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
            model=AzureOpenAI(
                id="o4-mini",
                api_version="2024-12-01-preview",
            ),
            description=TECHNICAL_WIN_STRATEGY_PROMPT,
            knowledge=self.knowledge_base,
            # response_model=SlideOutline,
            # structured_outputs=True,
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
            debug_mode=False,
            markdown=True,
        )

    def run(self, input_summary: str) -> Iterator[RunResponse]:

        # 1) Planning
        logger.info("working on the planning phase")
        logger.info("Generating Solutioning Narration")
        story_narration = self.story_narration_outline.run(input_summary)
        story_narration_outline = story_narration.content
        logger.info(f"Solutioning Narration Generated")

        # 2) Slide Outline Generation
        logger.info("Generating Slide Outline")
        slide_outline = self.generate_outline.run(
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

        logger.info("Generating Slide Content")
        for slide in slide_outline.content.slide_outline:
            slide_content = self.generate_slide_content.run(
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
                """
            )

            output_format = {
                "slide_number": slide.slide_number,
                "slide_title": slide.slide_title,
                "slide_content": slide_content.content,
            }
            yield RunResponse(
                event=RunEvent.run_response,
                content=output_format,
            )
        logger.info(f"Slide Content Generated")


# if __name__ == "__main__":
#     workflow = SlideSummaryGenerator()

#     for response in workflow.run(input_summary=meeting_summary):
#         print(response.content)
