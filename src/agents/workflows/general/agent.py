"""General agent for handling user queries with search and deal context tools."""

import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

from agno.agent import Agent as AgnoAgent
from agno.models.azure.openai_chat import AzureOpenAI
from agno.skills import LocalSkills, Skills
from dotenv import load_dotenv
from src.agents.base import Agent as BaseAgent
from src.agents.common.agent_tasks_extractor import AgentTasksExtractor
from src.agents.common.json_extractor import JsonExtractor
from src.agents.common.metadata_parser import MetadataParser
from src.agents.common.stream_buffer import StreamingAnswerBuffer
from src.agents.context import AgentContext
from src.agents.tools.registry import get_tools_for_agent
from src.agents.tools.transparent_tools import AgentCapabilities
from src.agents.utils import prepare_messages
from src.agents.workflows.general.prompts.general_prompt import get_general_prompt
from src.domain.models.organisation_tables import ChatMessage
from src.infra.config.agent_config import agent_config
from src.infra.messaging.chat_stream_models import AnswerDelta, Finalize, Frame, ToolUsage
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
        super().__init__(context=context)
        self.capabilities: AgentCapabilities | None = None
        self.json_extractor = JsonExtractor()
        self.metadata_parser = MetadataParser(self.json_extractor)
        self.separators = ["⟦AEP:1⟧"]

        # Get all tools for GeneralAgent from the registry
        self._tools: list[Callable[..., Any]] = get_tools_for_agent(
            "GeneralAgent", context, lambda: self.capabilities
        )

        # Generate prompt with org_name from context (falls back to "Our Organization" if None)
        system_prompt = get_general_prompt(context.org_name)

        # Load skills from src/agents/skills using Agno progressive discovery.
        skills_path = Path(__file__).parent.parent.parent / "skills"
        skills = Skills(loaders=[LocalSkills(str(skills_path))])

        # Get model config
        config = agent_config.get_config("general_agent")

        self.general_agent = AgnoAgent(
            model=AzureOpenAI(id=config.model, api_version=config.api_version),
            description=system_prompt,
            structured_outputs=False,
            debug_mode=config.debug_mode,
            markdown=True,
            tools=self._tools,
            skills=skills,
        )

    def run_stream(
        self, message_list: Sequence[ChatMessage | dict[str, Any]]
    ) -> AsyncIterator[Frame]:
        """Run with enhanced JSON streaming and transparency support."""

        async def _run() -> AsyncIterator[Frame]:
            processed_messages = prepare_messages(message_list)
            message_id = str(uuid.uuid4())

            # Use a simple list for frame queue - tools append synchronously
            # The "tick" events from StreamBuffer ensure we drain frequently
            frame_queue: list[Frame] = []
            self.capabilities = self._setup_capabilities(message_id, frame_queue)

            try:
                # Agno V2: arun(stream=True) returns async generator directly (no await)
                chat_response = self.general_agent.arun(input=processed_messages, stream=True)

                buffer = StreamingAnswerBuffer(self.separators, logger=logger)
                async for frame in self._process_streaming_response(
                    chat_response, buffer, frame_queue
                ):
                    yield frame

            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.exception("Error in enhanced run_workflow: %s", exc)
            finally:
                for frame in self._finalize_stream(frame_queue):
                    yield frame

        return _run()

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
        # Extractor for inline AGENT_TASKS blocks
        tasks_extractor = AgentTasksExtractor()

        async for kind, content, json_started in buffer.stream(chat_response):
            # Drain any pending frames from tool execution
            # This runs on every iteration including "tick" events (filtered tool chunks)
            while frame_queue:
                yield frame_queue.pop(0)

            # Handle different chunk types
            if kind == "tick":
                # Just a signal to drain the queue, no content to emit
                continue
            elif kind == "skill_tool":
                # Skill tool event detected by StreamingAnswerBuffer.
                # Emit a ToolUsage frame so skill calls appear alongside regular tools.
                yield ToolUsage(
                    assistant_message_id=self.capabilities.message_id if self.capabilities else "",
                    tool_name=content["tool_name"],
                    tool_description=f"Skill: {content['tool_args'].get('skill_name', '')}",
                    status=content["status"],
                    parameters=content["tool_args"],
                )
                continue
            elif kind == "answer" and content:
                # Extract AGENT_TASKS blocks from answer content
                result = tasks_extractor.process(content)

                # Emit any task frames that were extracted
                for task_frame in result.frames:
                    yield task_frame

                # Emit cleaned content (without AGENT_TASKS blocks)
                if result.clean_content:
                    yield AnswerDelta(content=result.clean_content)
            elif kind == "final":
                # Flush any remaining content from extractor
                flush_result = tasks_extractor.flush()
                for task_frame in flush_result.frames:
                    yield task_frame
                if flush_result.clean_content:
                    yield AnswerDelta(content=flush_result.clean_content)

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
