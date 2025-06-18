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

        # Convert messages to dict format for processing
        processed_messages = []
        
        for message in message_list:
            message_dict = Message(role=message.role, content=message.content)

            if message.role == "user" and message.attachments:
                # Process attachments if the message is from the user
                attachment_content = await self._process_attachments(message.attachments)
                message_dict.content += attachment_content
            processed_messages.append(message_dict)

        # Planning Execution
        execution_response = await self.execution_agent.arun(messages=processed_messages, stream=True)
        async for chunk in execution_response:
            if chunk.event == "RunResponseContent":
                yield RunResponse(status="RUNNING", content=chunk.content)
        yield RunResponse(content="", status="COMPLETED")

    async def _process_attachments(self, attachments: List[dict]) -> str:
        """
        Process a list of attachments and return their combined content.
        
        Args:
            attachments: List of attachment dictionaries
            
        Returns:
            str: Combined content from all attachments
            
        Raises:
            Exception: If there's an error processing any attachment
        """
        attachment_content = ""
        for attachment_dict in attachments:
            # Convert dict to AttachmentModel if it's not already
            attachment = (
                attachment_dict if isinstance(attachment_dict, AttachmentModel)
                else AttachmentModel(**attachment_dict)
            )
            file_url = str(attachment.url)
            # Read the document content using cloud-agnostic document reader asynchronously
            content = await asyncio.to_thread(self.document_reader.read_document, file_url)
            attachment_content += f"\nContent from {attachment.filename}:\n{content}\n"
            logger.info(f"Processed attachment from message: {attachment.filename}")
        
        return attachment_content


# async def main():
#     messages = [Message(role="user", content="How xeno can help pizza hut?")]
#     chat = GeneralChatAgent()

#     logger.info("Workflow execution started.")
#     async for response in chat.run_workflow(messages):
#         pass

# if __name__ == "__main__":
#     asyncio.run(main())
