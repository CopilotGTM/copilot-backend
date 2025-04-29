from pydantic import BaseModel, Field
from typing import List, Dict, AsyncIterator, Iterator
from textwrap import dedent

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

class QueryReformulation(BaseModel):
    """The reformulated query model"""
    reformulated_query: str = Field(
        description="Refomulated the query for next steps with better context of history"
    )
    query_context: str = Field(description="The context of the query")
    is_greeting: bool = Field(description="Whether the reformulated query is a greeting")

class QueryPlanning(BaseModel):
    """A list of objects where each object represents one step and contains required tool."""
    plan: str = Field(
        description="List of Steps that need to be followed by the agent to complete the task"
    )

class GeneralChatAgent(Workflow):

    def __init__(self, provider: str = "azure", model_name: str = "o3-mini", **kwargs):
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

        self.plan_execution_agent = Agent(
            model=AzureOpenAI(
                id="o3-mini",
                api_version="2024-12-01-preview",
            ),
            description=dedent("""
                You are a smart presales expert who has spent years in the field.
                Based on the provided plan and query please execute the Plan and Generate the response.
                Use the tools in the plan to get the information and generate a response.
            """),
            tools=[
                DuckDuckGoTools(),
                WikipediaTools(),
            ],
            knowledge=self.knowledge_base,
            # response_model=QueryPlanning,
            # structured_outputs=True,
            debug_mode=True,
            markdown=True
        )

    reformulation_agent = Agent(
        model=AzureOpenAI(
            id="o3-mini",
            api_version="2024-12-01-preview",
        ),
        description=dedent("""
            You are a query reformulation tool for downstream LLMs.
            Use historical context to rewrite the user's question as a single, concise query.
            Do NOT add explanations, examples, or background—output only the revised question itself.
        """),
        response_model=QueryReformulation,
        structured_outputs=True,
        debug_mode=False,
        markdown=True
    )

    planning_agent = Agent(
        model=AzureOpenAI(
            id="o3-mini",
            api_version="2024-12-01-preview",
        ),
        description=dedent("""
            You are a smart presales expert who has spent years in the field.
            You work for Xeno Organisation which have access to its capabilities uisng PDFKnowledgeBase.
            Based on the list of provided tools, plan the next steps for the query.

            List of Tools:
                1. DuckDuckGoTools: A tool for searching the web using DuckDuckGo.
                2. WikipediaTools: A tool for searching Wikipedia articles.
                3. PDFKnowledgeBase: A tool for searching a knowledge base Xeno's organization's documents.
              
            Things to Consider. 
            1. If user refers to a specific our organisation, by that means Xeno's organization.
        """),
        response_model=QueryPlanning,
        structured_outputs=True,
        debug_mode=False,
        markdown=True
    )

    def run(self, message_list: List[Dict[str, str]]) -> Iterator[RunResponse]:

        # 1) Reformulation
        reformulation_response = self.reformulation_agent.run(
            messages=message_list
        )

        # 2) Query Planning
        planning_response = self.planning_agent.run(
            messages=[{"role": "user", "content": reformulation_response.content.reformulated_query}]
        )
        logger.info(f"Query Planning Response: {planning_response.content}")

        # 3) Planning Execution
        execution_response = self.plan_execution_agent.run(
            messages=[{"role": "user", "content": planning_response.content.plan}]
        )
        yield RunResponse(
            event=RunEvent.run_response,
            content=execution_response.content
        )

# if __name__ == "__main__":
#     workflow = GeneralChatAgent()
#     messages = [
#         {
#             "role": "user",
#             "content": "What is the best way to get started with Xeno's organization?"
#         }
#     ]
#     for response in workflow.run(message_list=messages):
#         print(response.content)
