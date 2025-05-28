import asyncio
from typing import List, Dict, AsyncIterator
from uuid import UUID
from dotenv import load_dotenv

from agno.workflow import Workflow, RunResponse, RunEvent
from agno.agent import Agent
from agno.models.azure import AzureOpenAI
from agno.tools.duckduckgo import DuckDuckGoTools
from agno.tools.wikipedia import WikipediaTools
from agno.knowledge.pdf import PDFKnowledgeBase
from agno.vectordb.pgvector import PgVector
from agno.document.chunking.recursive import RecursiveChunking
from agno.embedder.azure_openai import AzureOpenAIEmbedder

from app_layer.utils.logger import get_logger
from workflows.utils.send_status import SendStatus
from workflows.general_chat.prompts.reformulation import REFORMULATION_PROMPT
from workflows.general_chat.prompts.planning import PLANNING_PROMPT
from workflows.general_chat.prompts.execution import EXECUTION_PROMPT
from workflows.general_chat.models import QueryReformulation
from workflows.general_chat.models import QueryPlanning
from workflows.utils.get_master_db_url import get_master_db_url
from workflows.utils.source_collection import KBHits
from workflows.utils.format_user_messages import format_user_messages
import json

load_dotenv()
logger = get_logger()


class GeneralChatAgent(Workflow):

    def __init__(
        self,
        provider: str = "azure",
        model_name: str = "o4-mini",
        sio=None,
        websocket_room=None,
        system_message_id: UUID = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.provider = provider
        self.model_name = model_name
        self.sio = sio
        self.room = websocket_room
        self.system_message_id = system_message_id

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

        self.reformulation_agent = Agent(
            model=AzureOpenAI(id="o4-mini", api_version="2024-12-01-preview"),
            description=REFORMULATION_PROMPT,
            response_model=QueryReformulation,
            structured_outputs=True,
            markdown=True,
        )

        self.planning_agent = Agent(
            model=AzureOpenAI(id="o4-mini", api_version="2024-12-01-preview"),
            description=PLANNING_PROMPT,
            response_model=QueryPlanning,
            structured_outputs=True,
            debug_mode=False,
            markdown=True,
        )

        self.plan_execution_agent = Agent(
            model=AzureOpenAI(id="o4-mini", api_version="2024-12-01-preview"),
            description=EXECUTION_PROMPT,
            tools=[DuckDuckGoTools(), WikipediaTools()],
            knowledge=self.knowledge_base,
            references_format="json",
            add_references=True,
            debug_mode=False,
            markdown=True,
            show_tool_calls=True,
        )

    async def run_workflow(self, message_list: List[Dict[str, str]]) -> AsyncIterator[RunResponse]:

        await SendStatus.send_chat_status(self, "started", "Workflow execution started.")

        # Reformulation
        reformulation_response: RunResponse = self.reformulation_agent.run(messages=message_list)
        if reformulation_response.content.is_greeting:
            yield RunResponse(
                event=RunEvent.workflow_completed, content=reformulation_response.content.greeting_response
            )
            return

        # Query Planning
        planning_response = self.planning_agent.run(
            messages=[{"role": "user", "content": reformulation_response.content.reformulated_query}]
        )
        logger.info(f"Query Planning Response: {planning_response.content}")

        # Planning Execution
        execution_response = await self.plan_execution_agent.arun(
            messages=[{"role": "user", "content": planning_response.content.plan}], stream=True
        )
        kb_hits = KBHits()
        async for chunk in kb_hits.stream(execution_response):
            yield RunResponse(event=RunEvent.run_response, content=chunk.content)

        yield RunResponse(event=RunEvent.workflow_completed, content="")


# async def main():
#     messages = [
#         {
#             "role": "user",
#             "content": "How xeno can help pizza hut?",
#         }
#     ]
#     chat = GeneralChatAgent()

#     logger.info("Workflow execution started.")
#     async for response in chat.run_workflow(messages):
#         logger.info(response.content)  # Prints each chunk directly

# if __name__ == "__main__":
#     asyncio.run(main())
