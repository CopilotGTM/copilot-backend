import asyncio
from typing import List, Dict, AsyncIterator
from uuid import UUID
from dotenv import load_dotenv
import os

from agno.workflow import Workflow, RunResponse
from agno.agent import Agent
from agno.models.azure import AzureOpenAI
from agno.tools.duckduckgo import DuckDuckGoTools
from agno.knowledge.pdf import PDFKnowledgeBase
from agno.vectordb.pgvector import PgVector
from agno.document.chunking.recursive import RecursiveChunking
from agno.embedder.azure_openai import AzureOpenAIEmbedder
from agno.agent.agent import Message
from agno.media import Image

from app_layer.utils.logger import get_logger
from workflows.utils.send_status import SendStatus
from workflows.general_chat.prompts.execution import EXECUTION_PROMPT
from workflows.utils.get_master_db_url import get_master_db_url
from infrastructure.document_reader import DocumentReader
from infrastructure.cloud_file_reader import CloudConfig

from models.chat_db_models import ChatMessage, AttachmentModel

load_dotenv()
logger = get_logger()

CLOUD_PROVIDER = os.getenv("CLOUD_PROVIDER")
APPLICATION_STORAGE_NAME = os.getenv("APPLICATION_STORAGE_NAME")
APPLICATION_REGION = os.getenv("APPLICATION_REGION")


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

        # Initialize document reader
        cloud_config = CloudConfig(CLOUD_PROVIDER, APPLICATION_REGION)
        self.document_reader = DocumentReader(cloud_config)

        self.df_url = get_master_db_url()
        self.knowledge_base = PDFKnowledgeBase(
            path="",
            vector_db=PgVector(
                table_name="knowledge_base",
                db_url=self.df_url,
                embedder=AzureOpenAIEmbedder(),
                schema="public"
            ),
            chunking_strategy=RecursiveChunking(),
        )
        self.execution_agent = Agent(
            model=AzureOpenAI(id="o4-mini", api_version="2024-12-01-preview"),
            description=EXECUTION_PROMPT,
            tools=[DuckDuckGoTools()],
            knowledge=self.knowledge_base,
            add_references=True,
            debug_mode=False,
            markdown=True,
            show_tool_calls=True,
        )

    async def run_workflow(self, message_list: List[ChatMessage]) -> AsyncIterator[RunResponse]:
        await SendStatus.send_chat_status(self, "started", "Workflow execution started.")

        processed_messages = []
        for message in message_list:
            message_dict = Message(role=message.role, content=message.content)
            if message.role == "user" and message.attachments:
                # process documents and images separately
                docs = await self._process_documents(message.attachments)
                imgs = self._extract_images(message.attachments)
                message_dict.content += docs
            processed_messages.append(message_dict)

        execution_response = await self.execution_agent.arun(
            messages=processed_messages, images=imgs, stream=True
        )
        async for chunk in execution_response:
            if chunk.event == "RunResponseContent":
                yield RunResponse(status="RUNNING", content=chunk.content)
        yield RunResponse(content="", status="COMPLETED")

    async def _process_documents(self, attachments: List[dict]) -> str:
        """
        Extract and return text content for PDF and DOCX attachments.
        """
        content_acc = ""
        for att in attachments:
            attachment = att if isinstance(att, AttachmentModel) else AttachmentModel(**att)
            mimetype = (attachment.mimetype or "").lower()
            if mimetype in (
                "application/pdf",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ):
                file_url = str(attachment.url)
                text = await asyncio.to_thread(self.document_reader.read_document, file_url)
                content_acc += f"\nContent from {attachment.filename}:\n{text}\n"
                logger.info(f"Processed document attachment: {attachment.filename}")
        return content_acc

    def _extract_images(self, attachments: List[dict]) -> List[Image]:
        """
        Return a list of Image instances for image attachments.
        """
        images: List[Image] = []
        for att in attachments:
            attachment = att if isinstance(att, AttachmentModel) else AttachmentModel(**att)
            mimetype = (attachment.mimetype or "").lower()
            if mimetype.startswith("image/"):
                images.append(Image(url=str(attachment.url)))
                logger.info(f"Extracted image attachment: {attachment.filename}")
        return images


# async def main():
#     messages = [Message(role="user", content="How xeno can help pizza hut?")]
#     chat = GeneralChatAgent()
#     async for response in chat.run_workflow(messages):
#         pass
# if __name__ == "__main__":
#     asyncio.run(main())
