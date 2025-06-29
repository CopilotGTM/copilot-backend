from typing import AsyncIterator
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

from workflows.generate_slide_outline.prompts.solution_narration import TECHNICAL_WIN_STRATEGY_PROMPT
from workflows.generate_slide_outline.prompts.slides_outline import SLIDE_OUTLINE_PROMPT
from workflows.generate_slide_outline.models import SlideItem, SlideOutline, SolutionNarration
from core.database import get_master_session

load_dotenv()
logger = get_logger()


class SlideContentGenerator(Workflow):

    def __init__(self, provider: str = "azure", model_name: str = "o4-mini", **kwargs):
        super().__init__(**kwargs)
        self.df_url = get_master_db_url()
        self.knowledge_base = PDFKnowledgeBase(
            path="",
            vector_db=PgVector(
                table_name="knowledge_base", db_url=self.df_url, embedder=AzureOpenAIEmbedder(), schema="public"
            ),
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

    async def run_workflow(self, input_summary: str) -> AsyncIterator[RunResponse]:

        # 1) Planning
        logger.info("Generating Solutioning Narration")
        story_narration = await self.story_narration_outline.arun(
            messages=[{"role": "user", "content": f"This is generated input Summary {input_summary}"}]
        )
        story_narration_outline = story_narration.content
        logger.info(f"{story_narration_outline}")

        yield RunResponse(
            content=story_narration_outline,
            status="RUNNING",
            content_type="SOLUTION_NARRATION"
        )

        # 2) Slide Outline Generation
        logger.info("Generating Slide Outline")
        slide_outline = await self.generate_outline.arun(
            messages=[
                {"role": "user", "content": f"Summary of the meeting:{input_summary}"},
                {"role": "assistant", "content": f"Solutioning Narration Suggested:{story_narration_outline}"},
            ]
        )

        for i in slide_outline.content.slide_outline:
            output_format = {
                "slide_number": i.slide_number,
                "slide_title": i.slide_title,
                "slide_content": i.description,
            }
            yield RunResponse(
                content=output_format,
                status="RUNNING",
                content_type="SOLUTION_OUTLINE"
            )


# async def main():

#     transcript_summary_agent = SlideContentGenerator()
#     logger.info("Workflow execution started.")
#     async for response in transcript_summary_agent.run_workflow(input_summary):
#         logger.info(response.content)  # Prints each chunk directly


# if __name__ == "__main__":
#     asyncio.run(main())
