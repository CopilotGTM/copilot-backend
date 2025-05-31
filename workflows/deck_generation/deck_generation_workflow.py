from typing import AsyncIterator, List, Dict, Any, Literal
import asyncio
import time
from pydantic import BaseModel, Field, ConfigDict

from agno.workflow import Workflow, RunResponse, RunEvent
from agno.agent import Agent
from agno.models.azure import AzureOpenAI

from agno.knowledge.pdf import PDFKnowledgeBase
from agno.vectordb.pgvector import PgVector
from agno.document.chunking.recursive import RecursiveChunking
from agno.embedder.azure_openai import AzureOpenAIEmbedder

from dotenv import load_dotenv
from app_layer.utils.logger import get_logger
from workflows.utils.get_master_db_url import get_master_db_url
from models.chat_db_models import ChatMessage as ChatMessageDB
from core.database import get_master_session
from sqlmodel import select
from models.embeddings_db_models import OrganisationDocument
from workflows.deck_generation.tools.solution_deck_search_tool import SolutionDeckSearchTool
from workflows.deck_generation.prompts.previous_slide_selection import SLIDE_SELECTION_PROMPT
load_dotenv()
logger = get_logger()


class DeckGeneration(Workflow):

    def __init__(self, provider: str = "azure", model_name: str = "o4-mini", **kwargs):
        super().__init__(**kwargs)
        self.df_url = get_master_db_url()
        self.presentation_base = PDFKnowledgeBase(
            path="",
            vector_db=PgVector(
                table_name="knowledge_base", 
                db_url=self.df_url, 
                embedder=AzureOpenAIEmbedder(), 
                schema="public",
            ),
            chunking_strategy=RecursiveChunking(),
        )
        self.master_session = next(get_master_session())
        self.embedder = AzureOpenAIEmbedder()
        
        self.previous_slide_extraction:Agent = Agent(
            model=AzureOpenAI(id="o4-mini", api_version="2024-12-01-preview"),
            tools=[SolutionDeckSearchTool()],
            # knowledge=SolutionDeckSearchTool(),
            description=SLIDE_SELECTION_PROMPT,
            markdown=False,
            debug_mode=True,
        )

    async def run_workflow(self, deck_message: ChatMessageDB) -> AsyncIterator[RunResponse]:
        try:
            tech_details = deck_message["technical_deck_details"]
        except:
            tech_details = deck_message.technical_deck_details

        slide_outline = tech_details["slide_content_summary"]
        transcript_summary = tech_details["complete_transcript_summary"]

        for slide_section in slide_outline:
            logger
            output = self.previous_slide_extraction.run(
                message = f"""
                    #### Meeting Summary
                     {transcript_summary}

                    ### Section Outline
                     {slide_section}
                """
            )

            # Yield the response for this section
            yield RunResponse(
                content=output.content,
                event=RunEvent.run_response
            )

# async def main():
#     # Initialize the deck generation workflow
#     chat = DeckGeneration()
#     output = []
    
#     logger.info("Workflow execution started.")
    
#     # Run the workflow and collect responses
#     async for response in chat.run_workflow(deck_message):
#         logger.info(f"Received response: {response.content}")
#         output.append(response.content)
    
#     logger.info(f"Workflow completed. Total responses: {len(output)}")
#     return output

# if __name__ == "__main__":
#     test = asyncio.run(main())
