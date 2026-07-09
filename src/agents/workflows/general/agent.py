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
from src.agents.common.token_tracking import extract_token_usage
from src.agents.context import AgentContext
from src.agents.timezone import get_agno_datetime_settings
from src.agents.tools.registry import get_tools_for_agent
from src.agents.tools.transparent_tools import AgentCapabilities
from src.agents.utils import prepare_messages
from src.agents.workflows.general.prompts.general_prompt import get_general_prompt
from src.domain.models.organisation_tables import ChatMessage
from src.infra.config.agent_config import agent_config
from src.infra.messaging.chat_stream_models import (
    AnswerDelta,
    Finalize,
    Frame,
    TokenUsageFrame,
    ToolUsage,
)
from src.infra.utils.logger import get_logger

load_dotenv()
logger = get_logger()

# Sentinel object to signal the Agno stream has finished producing frames
_AGNO_DONE = object()
_TOOLS_DONE = object()


class _AnswerEmissionState:
    """Track emitted answer text so final full-answer replays are not emitted twice."""

    def __init__(self) -> None:
        self.emitted_text = ""

    def next_delta(self, content: str) -> str:
        if not content:
            return ""

        if self.emitted_text and content.strip() == self.emitted_text.strip():
            return ""

        self.emitted_text += content
        return content


class GeneralAgent(BaseAgent):
    """General agent for handling user queries with enhanced transparency."""

    def __init__(
        self,
        *,
        context: AgentContext,
    ):
        super().__init__(context=context)
        self.capabilities: AgentCapabilities | None = None
        self._frame_message_id: str | None = context.message_id
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
            model=AzureOpenAI(
                id=config.model,
                api_version=config.api_version,
                retries=config.retries,
                delay_between_retries=1,
                exponential_backoff=True,
                request_params={"parallel_tool_calls": True},
            ),
            description=system_prompt,
            structured_outputs=False,
            debug_mode=config.debug_mode,
            markdown=True,
            **get_agno_datetime_settings(context.metadata),
            tools=self._tools,
            tool_call_limit=150,
            skills=skills,
            telemetry=False,
        )

    def run_stream(
        self,
        message_list: Sequence[ChatMessage | dict[str, Any]],
        *,
        images: Sequence[Any] | None = None,
        attachment_text: str | None = None,
    ) -> AsyncIterator[Frame]:
        """Run with enhanced JSON streaming and transparency support."""

        logger.info(
            "GeneralAgent.run_stream: images=%d, attachment_text=%d chars",
            len(images or []),
            len(attachment_text or ""),
        )

        async def _run() -> AsyncIterator[Frame]:
            processed_messages = prepare_messages(message_list, attachment_text=attachment_text)
            frame_message_id = self._get_frame_message_id()

            # Use an asyncio.Queue so tool frames are available immediately,
            # even while Agno is blocked executing a tool function.
            frame_queue: asyncio.Queue[Frame | object] = asyncio.Queue()
            self.capabilities = self._setup_capabilities(frame_message_id, frame_queue)

            try:
                # Agno V2: arun(stream=True) returns async generator directly (no await)
                # stream_events=True enables RunCompleted event for token metrics
                chat_response = self.general_agent.arun(
                    input=processed_messages,
                    stream=True,
                    stream_events=True,
                    images=images,
                )

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

    def _get_frame_message_id(self) -> str:
        """Use the worker-provided assistant message id when available."""
        if not self._frame_message_id:
            self._frame_message_id = self.context.message_id or str(uuid.uuid4())
        return self._frame_message_id

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
        """Merge Agno output and transparent tool frames into one stream."""
        tasks_extractor = AgentTasksExtractor()
        output_queue: asyncio.Queue[Frame | object] = asyncio.Queue()
        agno_task = asyncio.create_task(
            self._produce_agno_frames(
                chat_response=chat_response,
                buffer=buffer,
                tasks_extractor=tasks_extractor,
                output_queue=output_queue,
            )
        )
        tool_task = asyncio.create_task(
            self._forward_tool_frames(frame_queue=frame_queue, output_queue=output_queue)
        )

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
                if item is _TOOLS_DONE:
                    tool_done = True
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

    async def _produce_agno_frames(
        self,
        *,
        chat_response,
        buffer: StreamingAnswerBuffer,
        tasks_extractor: AgentTasksExtractor,
        output_queue: asyncio.Queue[Frame | object],
    ) -> None:
        """Consume Agno events and normalize them into chat frames."""
        answer_state = _AnswerEmissionState()
        try:
            async for kind, content, json_started in buffer.stream(chat_response):
                await self._handle_stream_chunk(
                    kind=kind,
                    content=content,
                    json_started=json_started,
                    tasks_extractor=tasks_extractor,
                    answer_state=answer_state,
                    output_queue=output_queue,
                )
        except Exception as exc:
            logger.exception("Agno producer error: %s", exc)
        finally:
            await output_queue.put(_AGNO_DONE)

    async def _handle_stream_chunk(
        self,
        *,
        kind: str,
        content: Any,
        json_started: bool,
        tasks_extractor: AgentTasksExtractor,
        answer_state: _AnswerEmissionState,
        output_queue: asyncio.Queue[Frame | object],
    ) -> None:
        """Translate one buffered Agno event into one or more frames."""
        if kind == "tick":
            return

        if kind == "skill_tool":
            await output_queue.put(self._build_skill_tool_frame(content))
            return

        if kind == "run_completed":
            logger.info("GeneralAgent received run_completed event, extracting token usage")
            usage = extract_token_usage(content, agent_name="GeneralAgent")
            if usage:
                frame = TokenUsageFrame(
                    assistant_message_id=self._get_frame_message_id(),
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    total_tokens=usage.total_tokens,
                    reasoning_tokens=usage.reasoning_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                    cost=usage.cost,
                    model_name=usage.model_name,
                    duration_seconds=usage.duration_seconds,
                    time_to_first_token=usage.time_to_first_token,
                )
                logger.info(
                    "GeneralAgent emitting TokenUsageFrame: model=%s, total_tokens=%d, cost=%s",
                    usage.model_name,
                    usage.total_tokens,
                    usage.cost,
                )
                await output_queue.put(frame)
            else:
                logger.warning("GeneralAgent run_completed but extract_token_usage returned None")
            return

        if kind == "answer" and content:
            await self._enqueue_answer_content(
                content=content,
                tasks_extractor=tasks_extractor,
                answer_state=answer_state,
                output_queue=output_queue,
            )
            return

        if kind == "final":
            await self._enqueue_flush_frames(
                tasks_extractor=tasks_extractor,
                answer_state=answer_state,
                output_queue=output_queue,
            )
            await self._enqueue_final_metadata(
                content=content,
                json_started=json_started,
                answer_state=answer_state,
                output_queue=output_queue,
            )

    def _build_skill_tool_frame(self, content: dict[str, Any]) -> ToolUsage:
        skill_name = content["tool_args"].get("skill_name", "")
        return ToolUsage(
            assistant_message_id=self._get_frame_message_id(),
            tool_name=content["tool_name"],
            tool_description=f"Skill: {skill_name}",
            status=content["status"],
            parameters=content["tool_args"],
        )

    async def _enqueue_answer_content(
        self,
        *,
        content: str,
        tasks_extractor: AgentTasksExtractor,
        answer_state: _AnswerEmissionState,
        output_queue: asyncio.Queue[Frame | object],
    ) -> None:
        result = tasks_extractor.process(content)
        for task_frame in result.frames:
            await output_queue.put(task_frame)
        clean_content = answer_state.next_delta(result.clean_content)
        if clean_content:
            await output_queue.put(AnswerDelta(content=clean_content))

    async def _enqueue_flush_frames(
        self,
        *,
        tasks_extractor: AgentTasksExtractor,
        answer_state: _AnswerEmissionState,
        output_queue: asyncio.Queue[Frame | object],
    ) -> None:
        flush_result = tasks_extractor.flush()
        for task_frame in flush_result.frames:
            await output_queue.put(task_frame)
        clean_content = answer_state.next_delta(flush_result.clean_content)
        if clean_content:
            await output_queue.put(AnswerDelta(content=clean_content))

    async def _enqueue_final_metadata(
        self,
        *,
        content: str,
        json_started: bool,
        answer_state: _AnswerEmissionState,
        output_queue: asyncio.Queue[Frame | object],
    ) -> None:
        if not content:
            return

        if not json_started:
            clean_content = answer_state.next_delta(content)
            if clean_content:
                await output_queue.put(AnswerDelta(content=clean_content))
            return

        for frame in self.metadata_parser.parse(content):
            await output_queue.put(frame)

    async def _forward_tool_frames(
        self,
        *,
        frame_queue: asyncio.Queue[Frame | object],
        output_queue: asyncio.Queue[Frame | object],
    ) -> None:
        """Forward tool frames from frame_queue to output_queue in real-time."""
        try:
            while True:
                frame = await frame_queue.get()
                if frame is _AGNO_DONE:
                    break
                await output_queue.put(frame)
        finally:
            await output_queue.put(_TOOLS_DONE)

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
