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

    async def run_stream(
        self, message_list: List[ChatMessage]
    ) -> AsyncIterator[Frame]:
        """Run with JSON streaming support."""
        logger.info("Starting workflow execution with JSON streaming")
        processed_messages = prepare_messages(message_list)
        message_id = str(uuid.uuid4())
        
        separator = "---JSON_METADATA---"
        buffer = ""
        json_part_started = False
        
        try:
            # Start the chat response
            chat_response = await self.general_agent.arun(
                messages=processed_messages, stream=True
            )

            async for chunk in chat_response:
                if not hasattr(chunk, "content") or not chunk.content:
                    continue

                # If we've already found the separator, just buffer the JSON part
                if json_part_started:
                    buffer += chunk.content
                    continue

                buffer += chunk.content
                
                # Check if the separator is now in the buffer
                if separator in buffer:
                    answer_part, json_part = buffer.split(separator, 1)
                    if answer_part:
                        yield AnswerDelta(message_id=message_id, content=answer_part)
                    
                    json_part_started = True
                    buffer = json_part
                else:
                    # To avoid sending a partial separator, we hold back a small part of the buffer
                    # and only send the content that is safe to send.
                    send_up_to = len(buffer) - (len(separator) - 1)
                    if send_up_to > 0:
                        to_send = buffer[:send_up_to]
                        buffer = buffer[send_up_to:]
                        yield AnswerDelta(message_id=message_id, content=to_send)

            # After the loop, if the separator was never found, the whole buffer is the answer.
            if not json_part_started and buffer:
                yield AnswerDelta(message_id=message_id, content=buffer)

            # Now parse the JSON metadata
            if json_part_started and buffer:
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

        except Exception as e:
            logger.error(f"Error in run_workflow: {e}")
        finally:
            yield Finalize(message_id=message_id, reason="done")
