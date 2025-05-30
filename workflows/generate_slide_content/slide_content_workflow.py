from typing import List, Dict, AsyncIterator
from agno.workflow import Workflow, RunResponse, RunEvent
from agno.agent import Agent
from agno.models.azure import AzureOpenAI
import asyncio

from agno.knowledge.pdf import PDFKnowledgeBase
from agno.vectordb.pgvector import PgVector
from agno.document.chunking.recursive import RecursiveChunking
from agno.embedder.azure_openai import AzureOpenAIEmbedder

from dotenv import load_dotenv
from app_layer.utils.logger import get_logger
from workflows.utils.get_master_db_url import get_master_db_url

from workflows.generate_slide_content.prompts.solution_narration import TECHNICAL_WIN_STRATEGY_PROMPT
from workflows.generate_slide_content.prompts.slides_outline import SLIDE_OUTLINE_PROMPT
from workflows.generate_slide_content.prompts.slide_content import SLIDE_CONTENT_PROMPT
from workflows.generate_slide_content.models import SlideItem, SlideOutline, SolutionNarration
from core.database import get_master_session

load_dotenv()
logger = get_logger()


class SlideContentGenerator(Workflow):

    def __init__(self, provider: str = "azure", model_name: str = "o4-mini", **kwargs):
        super().__init__(**kwargs)
        self.df_url = get_master_db_url()
        self.knowledge_base = PDFKnowledgeBase(
            path="",
            vector_db=PgVector(table_name="knowledge_base", db_url=self.df_url, embedder=AzureOpenAIEmbedder(), schema="public"),
            chunking_strategy=RecursiveChunking(),
        )
        self.master_session = next(get_master_session())

        self.story_narration_outline = Agent(
            model=AzureOpenAI(id="o4-mini", api_version="2025-04-01-preview", reasoning_effort="high"),
            description=TECHNICAL_WIN_STRATEGY_PROMPT,
            knowledge=self.knowledge_base,
            debug_mode=False,
            markdown=True,
        )

        self.generate_outline = Agent(
            model=AzureOpenAI(id="o4-mini", api_version="2025-04-01-preview"),
            description=SLIDE_OUTLINE_PROMPT,
            knowledge=self.knowledge_base,
            response_model=SlideOutline,
            reasoning=True,
            structured_outputs=True,
            debug_mode=False,
            markdown=True,
        )

        self.generate_slide_content = Agent(
            model=AzureOpenAI(id="o4-mini", api_version="2025-04-01-preview"),
            description=SLIDE_CONTENT_PROMPT,
            knowledge=self.knowledge_base,
            markdown=True,
        )

    async def run_workflow(self, input_summary: str) -> AsyncIterator[RunResponse]:

        # 1) Planning
        logger.info("Generating Solutioning Narration")
        story_narration = await self.story_narration_outline.arun(input_summary)
        story_narration_outline = story_narration.content
        logger.info(f"{story_narration_outline}")

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

        for i in slide_outline.content.slide_outline:
            output_format = {
                "slide_number": i.slide_number,
                "slide_title": i.slide_title,
                "slide_content": i.description,
            }
            yield RunResponse(
                event=RunEvent.run_response,
                content=output_format,
            )

        # previous_slides: List[Dict] = []

        # logger.info("Generating Slide Content")
        # for slide in slide_outline.content.slide_outline:
        #     logger.info(f"Slide Number: {slide.slide_number}, Slide Title: {slide.slide_title}")

        #     # Add previous slides to the prompt
        #     if previous_slides:
        #         previous_slides_str = "\n".join(
        #             [
        #                 f"Slide Number: {s['slide_number']}, Slide Title: {s['slide_title']}, Slide Content: {s['slide_content']}"
        #                 for s in previous_slides
        #             ]
        #         )
        #     else:
        #         previous_slides_str = ""
        #     slide_content = await self.generate_slide_content.arun(
        #         f"""
        #             Slide Number: {slide.slide_number},
        #             -----------------------------------------------------
        #             Proposed Slide Title: {slide.slide_title},
        #             -----------------------------------------------------
        #             Description: {slide.description},
        #             -----------------------------------------------------
        #             Meeting Summary: {input_summary}
        #             -----------------------------------------------------
        #             Solutioning Narration Suggested by Senior Sales Engineer: 
        #             {story_narration_outline}
        #             -----------------------------------------------------
        #             Data of Earlier Slides Created:
        #             {previous_slides_str}
        #         """
        #     )

        #     output_format = {
        #         "slide_number": slide.slide_number,
        #         "slide_title": slide.slide_title,
        #         "slide_content": slide_content.content,
        #     }

        #     # Append to our history before yielding
        #     previous_slides.append(output_format)

        yield RunResponse(
            event=RunEvent.run_response,
            content="",
        )
        # logger.info(f"Slide Content Generated")


async def main():

    transcript_summary_agent = SlideContentGenerator()
    logger.info("Workflow execution started.")
    async for response in transcript_summary_agent.run_workflow(input_summary):
        logger.info(response.content)  # Prints each chunk directly


if __name__ == "__main__":
    asyncio.run(main())
