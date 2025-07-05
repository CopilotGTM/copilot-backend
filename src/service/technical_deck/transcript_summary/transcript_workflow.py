from pydantic import BaseModel
import asyncio
from typing import AsyncIterator, List, Dict
from textwrap import dedent
import os
from agno.workflow import Workflow, RunResponse, RunEvent
from agno.agent import Agent
from agno.models.azure import AzureOpenAI

from dotenv import load_dotenv
from src.infra.utils.logger import get_logger
from src.infra.utils.get_master_db_url import get_master_db_url
from src.api.schemas.chat_socket_models import AttachmentModel

from src.infra.storage.document_reader import DocumentReader
from src.infra.storage.cloud_file_reader import CloudConfig
from src.infra.utils.app_state import fastapp
from src.service.technical_deck.transcript_summary.prompts.summary_prompt import TRANSCRIPT_SUMMARY_PROMPT

load_dotenv()
logger = get_logger()

CLOUD_PROVIDER = os.getenv("CLOUD_PROVIDER")
APPLICATION_STORAGE_NAME = os.getenv("APPLICATION_STORAGE_NAME")
APPLICATION_REGION = os.getenv("APPLICATION_REGION")


class TranscriptSummaryAgent(Workflow):

    def __init__(self, provider: str = "azure", model_name: str = "o4-mini", **kwargs):
        super().__init__(**kwargs)
        # Initialize the document reader with cloud configuration
        cloud_config = CloudConfig(CLOUD_PROVIDER, APPLICATION_REGION)
        self.document_reader = DocumentReader(cloud_config)

    summary_agent = Agent(
        model=AzureOpenAI(id="o4-mini", api_version="2024-12-01-preview", reasoning_effort="high"),
        description=TRANSCRIPT_SUMMARY_PROMPT,
        debug_mode=False,
        markdown=True,
    )

    async def run_workflow(self, attached_models: List[AttachmentModel]) -> AsyncIterator[RunResponse]:
        logger.info("Starting TranscriptSummaryAgent workflow")

        if not attached_models:
            raise ValueError("No attachments provided.")

        try:
            complete_content = ""
            # Iterate through each attached file
            for file_metadata in attached_models:
                file_url = str(file_metadata["url"])
                logger.info(f"Processing file: {file_metadata['filename']}")
                complete_content += self.document_reader.read_document(file_url)

            summary_response = await self.summary_agent.arun(complete_content, stream=True)
            
            async for chunk in summary_response:
                if chunk.event == "RunResponseContent":
                    yield RunResponse(status="RUNNING", content=chunk.content)

            yield RunResponse(content="", status="COMPLETED")

        except Exception as e:
            logger.error(f"Error processing transcripts: {str(e)}")
            yield RunResponse(content=str(e), status="ERROR")


# async def main():
#     attachments = [
#         {
#             "filename": "Harika Discussion.docx",
#             "url": "https://gtm-dev-application.s3.amazonaws.com/uploads/8c67b20f-9305-4f93-9e14-895f39b30b83/c9ac5227-c27d-4676-bf3f-42b712775e45/ffda9a64-071e-4fd8-9852-3924785bc817/Harika%20Discussion.docx",
#             "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
#             "size_bytes": 1926221,
#             "uploaded_at": "2025-05-07T10:12:12.910Z",
#             "metadata": {}
#         }
#     ]

#     transcript_summary_agent = TranscriptSummaryAgent()

#     logger.info("Workflow execution started.")
#     async for response in transcript_summary_agent.run_workflow(attachments):
#         logger.info(response.content)  # Prints each chunk directly


# if __name__ == "__main__":
#     asyncio.run(main())
