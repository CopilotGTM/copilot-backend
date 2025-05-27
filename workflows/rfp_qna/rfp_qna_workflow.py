from pydantic import BaseModel, Field
from typing import List, Dict, AsyncIterator, Iterator, Literal
from textwrap import dedent
import time

from agno.workflow import Workflow, RunResponse, RunEvent
from agno.agent import Agent
from agno.models.azure import AzureOpenAI
from agno.tools.duckduckgo import DuckDuckGoTools
from agno.tools.wikipedia import WikipediaTools

from agno.knowledge.pdf import PDFKnowledgeBase
from agno.vectordb.pgvector import PgVector
from agno.document.chunking.recursive import RecursiveChunking
from agno.embedder.azure_openai import AzureOpenAIEmbedder

from dotenv import load_dotenv
from app_layer.utils.logger import get_logger
from workflows.utils.get_master_db_url import get_master_db_url

load_dotenv()
logger = get_logger()


class QueryPlanning(BaseModel):
    """A list of objects where each object represents one step and contains required tool."""

    plan: str = Field(description="List of Steps that need to be followed by the agent to complete the task")


class OutputAnswer(BaseModel):
    """The output answer model"""

    answer: str = Field(description="The final answer to the user's question")
    confidence_score: float = Field(description="The confidence score of the answer")
    confidence_level: Literal["high", "medium", "low"] = Field(description="The confidence level of the answer")


class RfpAgent(Workflow):

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
        self.model = AzureOpenAI(id="o4-mini", api_version="2024-12-01-preview")

        self.plan_execution_agent = Agent(
            model=AzureOpenAI(
                id="o4-mini",
                api_version="2024-12-01-preview",
            ),
            description=dedent(
                """
                You are a smart presales expert who has spent years in the field.
                Based on the provided plan and query please execute the Plan and Generate the response.
                Use the tools in the plan to get the information and generate a response.
            """
            ),
            knowledge=self.knowledge_base,
            response_model=OutputAnswer,
            structured_outputs=True,
            debug_mode=False,
            markdown=False,
        )

        self.planning_agent = Agent(
            model=self.model,
            description=dedent(
                """
                You are a smart presales expert who has spent years in the field.
                You work for Xeno Organisation which have access to its capabilities uisng PDFKnowledgeBase.
                Based on the list of provided tools, plan the next steps for the query.

                List of Tools:
                    1. DuckDuckGoTools: A tool for searching the web using DuckDuckGo.
                    2. WikipediaTools: A tool for searching Wikipedia articles.
                    3. PDFKnowledgeBase: A tool for searching a knowledge base Xeno's organization's documents.
                
                Things to Consider. 
                1. If user refers to a specific our organisation, by that means Xeno's organization.
            """
            ),
            response_model=QueryPlanning,
            structured_outputs=True,
            debug_mode=False,
            markdown=False,
        )

    def run(self, input_message: str) -> RunResponse:
        try:
            start_time = time.time()
            logger.info("Starting query planning phase")

            # 2) Query Planning
            planning_response = self.planning_agent.run(messages=[{"role": "user", "content": input_message}])
            planning_time = time.time() - start_time
            logger.info(f"Query planning completed in {planning_time:.2f} seconds")

            # 3) Planning Execution
            execution_start = time.time()
            logger.info("Starting plan execution phase")
            execution_response = self.plan_execution_agent.run(
                messages=[{"role": "user", "content": planning_response.content.plan}]
            )
            execution_time = time.time() - execution_start
            logger.info(f"Plan execution completed in {execution_time:.2f} seconds")

            total_time = time.time() - start_time
            logger.info(f"Total processing time: {total_time:.2f} seconds")

            return RunResponse(event=RunEvent.run_response, content=execution_response.content.model_dump())

        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("GeneralChatAgent failure")
            return RunResponse(
                event=RunEvent.run_response,
                content={
                    "answer": "Sorry, I hit an unexpected error while processing your request.",
                    "confidence_score": 0.0,
                    "confidence_level": "low",
                    "error": str(exc),
                },
            )


# if __name__ == "__main__":
#     workflow = GeneralChatAgent()
#     messages = [
#         {
#             "role": "user",
#             "content": "How xeno can help pizza hut?",
#         }
#     ]
#     response = workflow.run(message_list=messages)
#     print(response.content)
