"""General agent for handling user queries with search and deal context tools."""

import asyncio
import contextlib
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

# Sentinel object to signal the Agno stream has finished producing frames
_AGNO_DONE = object()


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
        if context.metadata.get("source") == "scheduled_alert":
            system_prompt += (
                "\n\nYou are executing a reminder that has already been created. "
                "Do not create, list, or cancel reminders during this run."
            )

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
            telemetry=False,
        )

    def run_stream(
        self, message_list: Sequence[ChatMessage | dict[str, Any]]
    ) -> AsyncIterator[Frame]:
        """Run with enhanced JSON streaming and transparency support."""

        async def _run() -> AsyncIterator[Frame]:
            processed_messages = prepare_messages(message_list)
            message_id = str(uuid.uuid4())

            # Use an asyncio.Queue so tool frames are available immediately,
            # even while Agno is blocked executing a tool function.
            frame_queue: asyncio.Queue[Frame | object] = asyncio.Queue()
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

    def _setup_capabilities(self, message_id: str, frame_queue: asyncio.Queue):
        capabilities = AgentCapabilities(message_id)

        def emit_frame(frame: Frame):
            # Put frame into asyncio.Queue so it can be consumed immediately
            # by the concurrent drain task, even while Agno is blocked on tool execution.
            frame_queue.put_nowait(frame)

        capabilities.set_frame_emitter(emit_frame)
        return capabilities

    async def _process_streaming_response(
        self,
        chat_response,
        buffer: StreamingAnswerBuffer,
        frame_queue: asyncio.Queue,
    ):
        # Extractor for inline AGENT_TASKS blocks
        tasks_extractor = AgentTasksExtractor()

        # Merged output queue: both Agno stream frames and tool frames go here
        output_queue: asyncio.Queue[Frame | object] = asyncio.Queue()

        async def _agno_producer():
            """Consume the Agno stream and push processed frames to output_queue."""
            try:
                async for kind, content, json_started in buffer.stream(chat_response):
                    if kind == "tick":
                        continue
                    elif kind == "skill_tool":
                        await output_queue.put(
                            ToolUsage(
                                assistant_message_id=(
                                    self.capabilities.message_id if self.capabilities else ""
                                ),
                                tool_name=content["tool_name"],
                                tool_description=f"Skill: {content['tool_args'].get('skill_name', '')}",
                                status=content["status"],
                                parameters=content["tool_args"],
                            )
                        )
                    elif kind == "answer" and content:
                        result = tasks_extractor.process(content)
                        for task_frame in result.frames:
                            await output_queue.put(task_frame)
                        if result.clean_content:
                            await output_queue.put(AnswerDelta(content=result.clean_content))
                    elif kind == "final":
                        flush_result = tasks_extractor.flush()
                        for task_frame in flush_result.frames:
                            await output_queue.put(task_frame)
                        if flush_result.clean_content:
                            await output_queue.put(AnswerDelta(content=flush_result.clean_content))

                        if not json_started and content:
                            await output_queue.put(AnswerDelta(content=content))
                        elif json_started and content:
                            for frame in self.metadata_parser.parse(content):
                                await output_queue.put(frame)
            except Exception as exc:
                logger.exception("Agno producer error: %s", exc)
            finally:
                await output_queue.put(_AGNO_DONE)

        async def _tool_forwarder():
            """Forward tool frames from frame_queue to output_queue in real-time."""
            while True:
                frame = await frame_queue.get()
                if frame is _AGNO_DONE:
                    break
                await output_queue.put(frame)

        # Run both producers concurrently
        agno_task = asyncio.create_task(_agno_producer())
        tool_task = asyncio.create_task(_tool_forwarder())

        agno_done = False
        tool_done = False

        try:
            while not (agno_done and tool_done):
                item = await output_queue.get()
                if item is _AGNO_DONE:
                    agno_done = True
                    # Signal tool forwarder to stop
                    await frame_queue.put(_AGNO_DONE)
                    continue
                yield item
        finally:
            # Ensure tasks complete
            if not agno_task.done():
                agno_task.cancel()
            if not tool_task.done():
                # Signal tool forwarder to stop if still running
                await frame_queue.put(_AGNO_DONE)
                tool_task.cancel()

            # Await tasks to suppress unhandled exceptions
            for task in [agno_task, tool_task]:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _handle_final_content(self, buffer: str, json_part_started: bool):
        if not json_part_started and buffer:
            yield AnswerDelta(content=buffer)

        if json_part_started and buffer:
            for frame in self.metadata_parser.parse(buffer):
                yield frame

    def _finalize_stream(self, frame_queue: asyncio.Queue):
        # Drain any remaining frames (sync drain after async loop is done)
        while not frame_queue.empty():
            try:
                frame = frame_queue.get_nowait()
                if frame is not _AGNO_DONE:
                    yield frame
            except asyncio.QueueEmpty:
                break

        yield Finalize(reason="done")
