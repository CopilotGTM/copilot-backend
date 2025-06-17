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
from workflows.rfp_qna.models import OutputAnswer
from workflows.rfp_qna.prompts.execution import EXECUTION_PROMPT

load_dotenv()
logger = get_logger()


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

        self.execution_agent = Agent(
            model=AzureOpenAI(id="o4-mini", api_version="2024-12-01-preview"),
            description=EXECUTION_PROMPT,
            knowledge=self.knowledge_base,
            response_model=OutputAnswer,
            structured_outputs=True,
            debug_mode=False,
            markdown=False,
        )

    def run_workflow(self, input_message: str) -> RunResponse:
        try:
            execution_start = time.time()
            logger.info("Starting plan execution phase")
            execution_response = self.execution_agent.run(messages=[{"role": "user", "content": input_message}])
            execution_time = time.time() - execution_start
            logger.info(f"Total processing time: {execution_time:.2f} seconds")

            return RunResponse(status="COMPLETED", content=execution_response.content.model_dump())

        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("GeneralChatAgent failure")
            return RunResponse(
                status="COMPLETED",
                content={
                    "answer": "Sorry, I hit an unexpected error while processing your request.",
                    "confidence_score": 0.0,
                    "confidence_level": "low",
                    "error": str(exc),
                },
            )


# if __name__ == "__main__":
#     workflow = RfpAgent()
#     messages = "How xeno can help pizza hut?"
#     response = workflow.run_workflow(messages)
#     print(response.content)
