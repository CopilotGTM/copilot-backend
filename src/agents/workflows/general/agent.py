import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from agno.agent import Agent
from agno.models.azure import AzureOpenAI
from dotenv import load_dotenv
from src.agents.base import Agent as BaseAgent
from src.agents.common.json_extractor import JsonExtractor
from src.agents.common.metadata_parser import MetadataParser
from src.agents.common.stream_buffer import StreamingAnswerBuffer
from src.agents.context import AgentContext
from src.agents.tools.transparent_tools import AgentCapabilities
from src.agents.utils import prepare_messages
from src.agents.workflows.general.prompts.general_prompt import get_general_prompt
from src.agents.workflows.general.tools import (
    create_search_internal_knowledge,
    create_search_internet,
)
from src.api.schemas.chat_stream_models import AnswerDelta, Finalize, Frame
from src.domain.models.organisation_tables import ChatMessage
from src.infra.utils.logger import get_logger

load_dotenv()
logger = get_logger()


class GeneralAgent(BaseAgent):
    """General agent for handling user queries with enhanced transparency."""

    def __init__(
        self,
        *,
        context: AgentContext,
    ):
        self.capabilities: AgentCapabilities | None = None
        self.context = context
        logger.info(
            f"Initializing GeneralAgent with enhanced transparency features {self.context} "
        )
        self.json_extractor = JsonExtractor()
        self.metadata_parser = MetadataParser(self.json_extractor)
        self.separators = ["⟦AEP:1⟧"]

        # Create tool functions bound to context and capabilities getter
        self._tools: list[Callable[..., Any]] = [
            create_search_internal_knowledge(context, lambda: self.capabilities),
            create_search_internet(lambda: self.capabilities),
        ]

        # Log tool registration for debugging
        for tool_func in self._tools:
            logger.info(
                f"[TOOL REGISTERED] {tool_func.__name__}: {tool_func.__doc__[:100] if tool_func.__doc__ else 'No docstring'}..."
            )

        # Generate prompt with org_name from context (falls back to "Our Organization" if None)
        system_prompt = get_general_prompt(context.org_name)

        self.general_agent = Agent(
            model=AzureOpenAI(id="gpt-5", api_version="2024-12-01-preview"),
            description=system_prompt,
            structured_outputs=False,
            debug_mode=False,  # Disabled - tool usage tracked via ToolUsage frames
            show_tool_calls=False,  # Disabled - prevents tool call info in answer stream
            markdown=True,
            tools=self._tools,  # type: ignore[arg-type]
        )
        logger.info(f"[AGNO AGENT] Initialized with {len(self._tools)} tools")

    async def run_stream(self, message_list: list[ChatMessage]) -> AsyncIterator[Frame]:
        """Run with enhanced JSON streaming and transparency support."""
        logger.info("Starting enhanced workflow execution with transparency")
        logger.info(f"[AGNO AGENT] Processing {len(message_list)} messages")

        processed_messages = prepare_messages(message_list)
        message_id = str(uuid.uuid4())

        # Use a simple list for frame queue - tools append synchronously
        # The "tick" events from StreamBuffer ensure we drain frequently
        frame_queue: list[Frame] = []
        self.capabilities = self._setup_capabilities(message_id, frame_queue)

        try:
            logger.info("[AGNO AGENT] Calling arun with stream=True...")
            chat_response = await self.general_agent.arun(messages=processed_messages, stream=True)

            buffer = StreamingAnswerBuffer(self.separators, logger=logger)
            async for frame in self._process_streaming_response(chat_response, buffer, frame_queue):
                yield frame

        except Exception as exc:  # noqa: B902
            logger.error(f"Error in enhanced run_workflow: {exc}")
        finally:
            for frame in self._finalize_stream(frame_queue):
                yield frame

    def _setup_capabilities(self, message_id: str, frame_queue: list):
        capabilities = AgentCapabilities(message_id)

        def emit_frame(frame: Frame):
            # Sync append - tools call this during execution
            frame_queue.append(frame)

        capabilities.set_frame_emitter(emit_frame)
        return capabilities

    async def _process_streaming_response(
        self,
        chat_response,
        buffer: StreamingAnswerBuffer,
        frame_queue: list,
    ):
        async for kind, content, json_started in buffer.stream(chat_response):
            # Drain any pending frames from tool execution
            # This runs on every iteration including "tick" events (filtered tool chunks)
            while frame_queue:
                yield frame_queue.pop(0)

            # Handle different chunk types
            if kind == "tick":
                # Just a signal to drain the queue, no content to emit
                continue
            elif kind == "answer" and content:
                yield AnswerDelta(content=content)
            elif kind == "final":
                async for frame in self._handle_final_content(content, json_started):
                    yield frame

    async def _handle_final_content(self, buffer: str, json_part_started: bool):
        if not json_part_started and buffer:
            yield AnswerDelta(content=buffer)

        if json_part_started and buffer:
            for frame in self.metadata_parser.parse(buffer):
                yield frame

    def _finalize_stream(self, frame_queue: list):
        # Drain any remaining frames
        while frame_queue:
            yield frame_queue.pop(0)

        yield Finalize(reason="done")
