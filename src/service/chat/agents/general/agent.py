import asyncio
from typing import List
from uuid import UUID
from dotenv import load_dotenv
import os

from agno.workflow import Workflow
from agno.agent import Agent
from agno.models.azure import AzureOpenAI
from agno.agent.agent import Message

from src.infra.utils.logger import get_logger
from src.service.chat.agents.general.prompts.general_prompt import GENERAL_PROMPT
from src.domain.models.organisation_tables import ChatMessage
from src.service.chat.agents.models import ChatAgentOutput

load_dotenv()
logger = get_logger()

APPLICATION_REGION = os.getenv("APPLICATION_REGION")


class GeneralAgent(Workflow):
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

        # Structured-output agent (non-streaming only)
        self.general_agent = Agent(
            model=AzureOpenAI(id="o4-mini", api_version="2024-12-01-preview"),
            description=GENERAL_PROMPT,
            response_model=ChatAgentOutput,
            structured_outputs=True,
            debug_mode=False,
            markdown=False,
        )

    # Structured-only API
    async def run_structured(self, message_list: List[ChatMessage]) -> ChatAgentOutput:
        """Run once and return a validated ChatAgentOutput model."""
        processed_messages: List[Message] = []
        for message in message_list:
            # Extract plain text content if domain message stores JSON
            content = message.content
            if isinstance(content, dict):
                content = content.get("text", "")
            # Do not process attachments here; keep it fast and simple
            message_dict = Message(role=message.role, content=content)
            processed_messages.append(message_dict)

        def _run():
            return self.general_agent.run(messages=processed_messages)

        result = await asyncio.to_thread(_run)
        return result.content
