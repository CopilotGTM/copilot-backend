from typing import AsyncIterator
import asyncio
import time

from agno.workflow import Workflow, RunResponse, RunEvent

from agno.knowledge.pdf import PDFKnowledgeBase
from agno.vectordb.pgvector import PgVector
from agno.document.chunking.recursive import RecursiveChunking
from agno.embedder.azure_openai import AzureOpenAIEmbedder

from dotenv import load_dotenv
from app_layer.utils.logger import get_logger
from workflows.utils.get_master_db_url import get_master_db_url

load_dotenv()
logger = get_logger()


class DeckGeneration(Workflow):

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

    async def run_workflow(self) -> AsyncIterator[RunResponse]:

        slide = {
            "type": "cover",
            "data": {
                "title": "Xeno ",
                "text?": "Loyalty Construct and Benefits To Grow Repeat Revenue 💰💰💰  ",
                "image": "https://test-gtmcopilot.s3.us-east-1.amazonaws.com/test.png",
            },
        }
        slide_data = [slide for i in range(8)]
        for chunk in slide_data:
            time.sleep(1)
            yield RunResponse(event=RunEvent.run_response, content=chunk)


# async def main():
#     chat = DeckGeneration()

#     logger.info("Workflow execution started.")
#     async for response in chat.run_workflow():
#         logger.info(response.content)  # Prints each chunk directly

# if __name__ == "__main__":
#     asyncio.run(main())
