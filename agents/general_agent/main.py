# agents/general_agent/main.py
import os
from typing import Iterator, List, Dict
from dotenv import load_dotenv
import urllib.parse

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.models.azure import AzureOpenAI
from app_layer.utils.logger import get_logger

from core.database import get_master_session
from models.embeddings_db_models import OrganisationDocument

from agno.knowledge.pdf import PDFKnowledgeBase
from agno.vectordb.pgvector import PgVector
from agno.document.chunking.recursive import RecursiveChunking
from agno.embedder.azure_openai import AzureOpenAIEmbedder
from agents.general_agent.prompt import MAIN_PROMPT
from agno.tools.duckduckgo import DuckDuckGoTools
from agno.tools.wikipedia import WikipediaTools

logger = get_logger()

load_dotenv()

DATABASE_HOST = os.getenv("DATABASE_HOST", "localhost")
DATABASE_PORT = os.getenv("DATABASE_PORT", "5432")
DATABASE_NAME = os.getenv("DATABASE_NAME", "postgres")
DATABASE_USER = os.getenv("DATABASE_USER", "postgres")
DATABASE_PASSWORD = os.getenv("DATABASE_PASSWORD", "")

encoded_password = urllib.parse.quote_plus(DATABASE_PASSWORD)
db_url = f"postgresql://{DATABASE_USER}:{encoded_password}" f"@{DATABASE_HOST}:{DATABASE_PORT}/{DATABASE_NAME}"


class BasicChatAgent:
    def __init__(self, provider: str, model_name: str):
        self.master_session = next(get_master_session())
        self.model_id = model_name
        self.model = self._initialize_model(provider)
        self.conversation_history: List[Dict[str, str]] = []

    def _initialize_model(self, provider: str):
        """Initialize the appropriate model based on the provider."""
        if provider == "openai":
            model = OpenAIChat(id=self.model_id)
            logger.info("OpenAI model initialized")
            return model
        elif provider == "azure":
            model = AzureOpenAI(id="o3-mini", api_version="2024-12-01-preview")
            logger.info("Azure OpenAI model initialized")
            return model
        else:
            logger.error(f"Invalid provider: {provider}")
            raise ValueError("Provider must be 'openai' or 'azure'")

    def agent_invoke(self, messages: List[Dict[str, str]]) -> Iterator[str]:
        logger.info(f"Invoking agent with messages: {messages}")
        # Track conversation history
        self.conversation_history.extend(messages)

        # Build knowledge base and agent
        knowledge_base = PDFKnowledgeBase(
            path="",
            vector_db=PgVector(
                table_name="xeno_knowledge",
                db_url=db_url,
                embedder=AzureOpenAIEmbedder(),
            ),
            chunking_strategy=RecursiveChunking(),
        )

        agent = Agent(
            model=self.model,
            tools=[DuckDuckGoTools(), WikipediaTools()],
            knowledge=knowledge_base,
            description="""You are an expert Pre-Sales Assistant that first searches the knowledge base for information. Only when information isn't found in the knowledge base, you utilize external sources while being transparent about this fallback.
            You are also an expert in the field of Pre-Sales and can answer questions about the product, the company, the industry, and the market.""",
            instructions=[MAIN_PROMPT],
            show_tool_calls=False,
            debug_mode=False,
            markdown=False,
        )

        # Use conversation history directly for chat
        for run_response in agent.run(messages=self.conversation_history, stream=True):
            logger.info(f"Generated response chunk: {run_response.content}")
            yield run_response.content


# if __name__ == "__main__":
#     agent = BasicChatAgent(provider="azure", model_name="o3-mini")
#     for response in agent.agent_invoke(messages=[
#         {"role": "user", "content": "What is the capital of India?"},
#         {"role": "assistant", "content": "New Delhi"},
#         {"role": "user", "content": "Before THis"}],

#     ):
#         print(response)
