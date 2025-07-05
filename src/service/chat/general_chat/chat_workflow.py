import asyncio
from typing import List, AsyncIterator
from uuid import UUID
from dotenv import load_dotenv
import os
import json

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

from src.infra.utils.logger import get_logger
from src.infra.utils.send_status import SendStatus
from src.service.chat.general_chat.prompts.execution import EXECUTION_PROMPT
from src.infra.utils.get_master_db_url import get_master_db_url
from src.infra.storage.document_reader import DocumentReader
from src.infra.storage.cloud_file_reader import CloudConfig
from src.domain.models.chat_db_models import ChatMessage, AttachmentModel

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
                table_name="knowledge_base", db_url=self.df_url, embedder=AzureOpenAIEmbedder(), schema="public"
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

    async def run_workflow(self, message_list: List[ChatMessage]) -> AsyncIterator[RunResponse]:
        await SendStatus.send_chat_status(self, "Thinking...")

        processed_messages = []
        imgs = []  # Initialize imgs list
        for message in message_list:
            message_dict = Message(role=message.role, content=message.content)
            if message.role == "user" and message.attachments:
                # process documents and images separately
                docs = await self._process_documents(message.attachments)
                if docs:
                    message_dict.content += docs
                # Extract images if any
                image_list = self._extract_images(message.attachments)
                if image_list:
                    imgs.extend(image_list)
            processed_messages.append(message_dict)

        execution_response = await self.execution_agent.arun(messages=processed_messages, images=imgs, stream=True)

        async for chunk in execution_response:
            match chunk.event:
                case "RunResponseContent":
                    yield RunResponse(status="RUNNING", content=chunk.content)
                case "ToolCallStarted":
                    tool_name = chunk.tool.tool_name
                    if tool_name == "asearch_knowledge_base":
                        # Handle knowledge base search
                        query = chunk.tool.tool_args.get("query", "")
                        await SendStatus.send_chat_status(self, f"Searching Knowledge Base - {query}", "", [], "search")
                    elif tool_name == "duckduckgo_search":
                        # Handle DuckDuckGo search
                        query = chunk.tool.tool_args.get("query", "")
                        await SendStatus.send_chat_status(self, f"Searching Internet - {query}", "", [], "search")
                case "ToolCallCompleted":
                    tool_name = chunk.tool.tool_name
                    if tool_name == "asearch_knowledge_base":
                        # file filename mine type and link from search metadata
                        results = json.loads(chunk.tool.result)
                        seen = set()
                        sources = []
                        for idx, result in enumerate(results):
                            # Safely extract metadata and origin
                            metadata = result.get("meta_data")
                            if not metadata or "origin" not in metadata:
                                continue
                            orig = metadata["origin"]
                            key = (orig.get("filename"), orig.get("mimetype"), orig.get("uri"))
                            if key not in seen:
                                seen.add(key)
                                source_entry = {
                                    "name": orig.get("filename"),
                                    "mimetype": orig.get("mimetype"),
                                    "url": orig.get("uri"),
                                    "snippet": "",
                                }
                                sources.append(source_entry)
                        await SendStatus.send_chat_status(self, "Search Results", "", sources, "sources")
                    elif tool_name == "duckduckgo_search":
                        # Handle DuckDuckGo search results
                        results = json.loads(chunk.tool.result)
                        seen = set()
                        sources = []
                        for idx, result in enumerate(results):
                            title = result.get("title")
                            url = result.get("href")
                            snippet = result.get("snippet", "")
                            key = (title, url)
                            if title and url and key not in seen:
                                seen.add(key)
                                source_entry = {
                                    "name": title,
                                    "mimetype": "text/html",
                                    "url": url,
                                    "snippet": result.get("body", "")[:20],
                                }
                                sources.append(source_entry)
                        await SendStatus.send_chat_status(self, "Search Results", "", sources, "sources")
                case _:
                    logger.info(f"{chunk}")
                    pass
        yield RunResponse(content="", status="COMPLETED")


# async def main():
#     messages = [Message(role="user", content="How xeno can help pizza hut?")]
#     chat = GeneralChatAgent()
#     async for response in chat.run_workflow(messages):
#         pass
# if __name__ == "__main__":
#     asyncio.run(main())
