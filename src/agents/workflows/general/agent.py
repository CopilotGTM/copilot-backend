import json
import os
import uuid
from typing import AsyncIterator, List

from agno.agent import Agent
from agno.agent.agent import Message
from agno.models.azure import AzureOpenAI
from dotenv import load_dotenv

from src.api.schemas.chat_stream_models import (
    AnswerDelta,
    AnswerMetrics,
    Finalize,
    Frame,
    Suggestion,
    SuggestionsReplace,
)
from src.domain.models.organisation_tables import ChatMessage
from src.infra.utils.logger import get_logger
from src.agents.base import Agent as BaseAgent
from src.agents.utils import prepare_messages
from src.agents.workflows.general.prompts.general_prompt import GENERAL_PROMPT

load_dotenv()
logger = get_logger()

class GeneralAgent(BaseAgent):
    """General agent for handling user queries."""
    def __init__(self):
        self.general_agent = Agent(
            model=AzureOpenAI(id="gpt-5-mini", api_version="2024-12-01-preview"),
            description=GENERAL_PROMPT,
            structured_outputs=False,
            debug_mode=False,
            markdown=False,
        )

    def _parse_json_metadata(self, buffer, message_id):
        """Helper to parse JSON metadata and yield frames."""
        try:
            response_data = json.loads(buffer)
            if "confidence_score" in response_data:
                yield AnswerMetrics(
                    message_id=message_id,
                    confidence=response_data["confidence_score"],
                )
            if "suggested_questions" in response_data:
                suggestions = [
                    Suggestion(label=q)
                    for q in response_data.get("suggested_questions", [])
                ]
                yield SuggestionsReplace(message_id=message_id, items=suggestions)
        except json.JSONDecodeError:
            logger.error(f"Failed to parse JSON metadata: {buffer}")

    async def _stream_answer(self, chat_response, separator):
        """Helper to stream answer and buffer JSON part. Returns buffer and json_part_started."""
        buffer = ""
        json_part_started = False
        async for chunk in chat_response:
            if not hasattr(chunk, "content") or not chunk.content:
                continue

            if json_part_started:
                buffer += chunk.content
                continue

            buffer += chunk.content

            if separator in buffer:
                answer_part, json_part = buffer.split(separator, 1)
                yield ("answer", answer_part)
                json_part_started = True
                buffer = json_part
            else:
                send_up_to = len(buffer) - (len(separator) - 1)
                if send_up_to > 0:
                    to_send = buffer[:send_up_to]
                    buffer = buffer[send_up_to:]
                    yield ("answer", to_send)
        yield ("final", buffer, json_part_started)

    async def run_stream(
        self, message_list: List[ChatMessage]
    ) -> AsyncIterator[Frame]:
        """Run with JSON streaming support."""
        logger.info("Starting workflow execution with JSON streaming")
        processed_messages = prepare_messages(message_list)
        message_id = str(uuid.uuid4())
        separator = "---JSON_METADATA---"

        try:
            chat_response = await self.general_agent.arun(
                messages=processed_messages, stream=True
            )
            buffer = ""
            json_part_started = False
            async for result in self._stream_answer(chat_response, separator):
                if result[0] == "answer":
                    if result[1]:
                        yield AnswerDelta(message_id=message_id, content=result[1])
                elif result[0] == "final":
                    buffer = result[1]
                    json_part_started = result[2]

            if not json_part_started and buffer:
                yield AnswerDelta(message_id=message_id, content=buffer)

            if json_part_started and buffer:
                for frame in self._parse_json_metadata(buffer, message_id):
                    yield frame

        except Exception as e:
            logger.error(f"Error in run_workflow: {e}")
        finally:
            yield Finalize(message_id=message_id, reason="done")
