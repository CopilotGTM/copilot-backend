import json
import uuid
from collections.abc import AsyncIterator

from agno.agent import Agent
from agno.models.azure import AzureOpenAI
from dotenv import load_dotenv
from src.agents.base import Agent as BaseAgent
from src.agents.tools.transparent_tools import AgentCapabilities
from src.agents.utils import prepare_messages
from src.agents.workflows.general.prompts.general_prompt import GENERAL_PROMPT
from src.api.schemas.chat_stream_models import (
    AgentSwitch,
    AnswerDelta,
    AnswerMetrics,
    FieldSingleSelect,
    Finalize,
    Frame,
    QuestionPrompt,
    SourcesReplace,
    SuggestionsReplace,
)
from src.domain.models.organisation_tables import ChatMessage
from src.infra.utils.logger import get_logger

load_dotenv()
logger = get_logger()


class GeneralAgent(BaseAgent):
    """General agent for handling user queries with enhanced transparency."""

    def __init__(self):
        self.general_agent = Agent(
            model=AzureOpenAI(id="gpt-5-mini", api_version="2024-12-01-preview"),
            description=GENERAL_PROMPT,
            structured_outputs=False,
            debug_mode=False,
            markdown=True,
        )
        self.capabilities: AgentCapabilities | None = None

    def _extract_json_str(self, text: str) -> str | None:
        """Extract a JSON object substring from text robustly."""
        if not text:
            return None

        # Try sentinel-wrapped payload first
        json_str = self._extract_from_sentinel(text)
        if json_str:
            return json_str

        # Try fenced code block
        json_str = self._extract_from_fence(text)
        if json_str:
            return json_str

        # Fall back to bracket matching
        return self._extract_from_brackets(text)

    def _extract_from_sentinel(self, text: str) -> str | None:
        """Extract JSON from sentinel block ⟦AEP:1⟧ ... ⟦/AEP⟧."""
        S_START = "⟦AEP:1⟧"
        S_END = "⟦/AEP⟧"

        if S_START not in text or S_END not in text:
            return None

        try:
            segment = text.split(S_START, 1)[1].split(S_END, 1)[0]

            # Check if segment has fenced content
            if "```" in segment:
                return self._extract_from_fence(segment)

            # Use bracket matching within segment
            return self._extract_from_brackets(segment)
        except Exception:
            return None

    def _extract_from_fence(self, text: str) -> str | None:
        """Extract JSON from fenced code block."""
        if "```" not in text:
            return None

        parts = text.split("```")
        if len(parts) < 3:
            return None

        candidate = parts[1]
        return self._clean_language_hint(candidate)

    def _clean_language_hint(self, content: str) -> str:
        """Remove language hint from fenced content if present."""
        if "\n" not in content:
            return content.strip()

        first_line, rest = content.split("\n", 1)
        language_hints = {"json", "javascript", "ts", "yaml"}

        if first_line.strip().lower() in language_hints:
            return rest.strip()

        return content.strip()

    def _extract_from_brackets(self, text: str) -> str | None:
        """Extract JSON using bracket matching."""
        start = text.find("{")
        if start == -1:
            return None

        return self._match_json_brackets(text, start)

    def _match_json_brackets(self, text: str, start: int) -> str | None:
        """Match JSON brackets starting from given position."""
        depth = 0
        state = {"in_str": False, "escaped": False}

        for i, ch in enumerate(text[start:], start=start):
            if self._process_character(ch, state):
                continue

            # Process structural characters
            if ch == "{":
                depth += 1
            elif ch == "}" and depth > 0:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]

        return None

    def _process_character(self, ch: str, state: dict) -> bool:
        """Process character for string/escape state. Returns True if handled."""
        if state["in_str"]:
            if state["escaped"]:
                state["escaped"] = False
                return True
            elif ch == "\\":
                state["escaped"] = True
                return True
            elif ch == '"':
                state["in_str"] = False
                return True
            return True
        else:
            if ch == '"':
                state["in_str"] = True
                return True
        return False

    def _parse_json_metadata(self, buffer: str):
        """Helper to parse JSON metadata and yield frames."""
        json_text = self._extract_json_str(buffer)
        if not json_text:
            logger.error(f"Failed to locate JSON metadata in buffer: {buffer}")
            return

        try:
            response_data = json.loads(json_text)
            logger.info(f"Parsed JSON metadata: {response_data}")
            self._validate_metadata_version(response_data)

            # Yield various frame types based on metadata
            yield from self._yield_confidence_frame(response_data)
            yield from self._yield_suggestions_frame(response_data)
            yield from self._yield_sources_frame(response_data)
            yield from self._yield_question_frame(response_data)
            yield from self._yield_agent_switch_frame(response_data)

        except json.JSONDecodeError:
            logger.error(f"Failed to parse JSON metadata: {json_text}")

    def _validate_metadata_version(self, response_data: dict):
        """Validate metadata version."""
        if not isinstance(response_data, dict):
            return

        version = str(response_data.get("version", "")).lower()
        valid_versions = {"aep/1", "1", "v1", "aep:v1"}

        if version and version not in valid_versions:
            logger.warning(f"Unrecognized metadata version: {version}")

    def _yield_confidence_frame(self, response_data: dict):
        """Yield confidence frame if present."""
        if "confidence_score" in response_data:
            confidence = self._convert_confidence_score(response_data.get("confidence_score"))
            yield AnswerMetrics(confidence=confidence)

    def _yield_suggestions_frame(self, response_data: dict):
        """Yield suggestions frame if present."""
        if "suggested_questions" in response_data:
            suggestions = [str(q) for q in response_data.get("suggested_questions", [])]
            if suggestions:
                yield SuggestionsReplace(items=suggestions)

    def _yield_sources_frame(self, response_data: dict):
        """Yield sources frame if present."""
        sources = response_data.get("sources") or response_data.get("citations")
        if sources:
            yield SourcesReplace(sources=sources)

    def _yield_question_frame(self, response_data: dict):
        """Yield question prompt frame if present."""
        ask = response_data.get("ask")
        if not isinstance(ask, dict):
            return

        # Only create one question with the first field that has options
        field_specs = ask.get("fields", [])
        if field_specs:
            # Find the first field with options or use the first field
            main_field = None
            for spec in field_specs:
                if spec.get("options"):
                    main_field = self._create_single_select_field(spec)
                    break

            # If no field with options found, use the first field with fallback options
            if not main_field and field_specs:
                main_field = self._create_single_select_field(field_specs[0])

            if main_field:
                yield QuestionPrompt(
                    prompt=ask.get("prompt") or "",
                    fields=[main_field],  # Single field only
                    await_user_response=bool(ask.get("await_user_response", True)),
                )

    def _create_single_select_field(self, spec: dict) -> FieldSingleSelect | None:
        """Create a single select field from specification."""
        name = spec.get("name") or "input"
        required = bool(spec.get("required", False))

        # Single select fields must have options defined
        options = spec.get("options", [])
        if not options:
            # If no options provided, use basic fallback
            options = ["Option 1", "Option 2", "Option 3"]

        return FieldSingleSelect(
            name=name,
            options=[str(option) for option in options],
            required=required,
        )

    def _yield_agent_switch_frame(self, response_data: dict):
        """Yield agent switch frame if present."""
        # Check multiple possible keys for agent switch
        switch = (
            response_data.get("switch_agent")
            or response_data.get("agent_switch")
            or response_data.get("switch")
        )

        # Add debugging to see what we're getting
        logger.info(
            f"Checking for agent switch in response_data keys: {list(response_data.keys())}"
        )
        logger.info(f"Switch data found: {switch}")

        if isinstance(switch, dict) and switch.get("to"):
            logger.info(f"Agent switch detected - switching to: {switch.get('to')}")
            yield AgentSwitch(
                from_agent="GENERAL_CHAT",
                to_agent=switch.get("to"),
                reason=switch.get("reason") or "Agent switch requested",
            )
        elif isinstance(switch, str) and switch.strip():
            # Handle simple string format
            logger.info(f"Agent switch detected (string format) - switching to: {switch}")
            yield AgentSwitch(
                from_agent="GENERAL_CHAT",
                to_agent=switch.strip(),
                reason="Agent switch requested",
            )
        else:
            # Check if there are any other fields that might indicate agent switching
            if any(key.lower().startswith(("switch", "agent")) for key in response_data):
                logger.info(
                    f"Potential agent switch fields found but not in expected format: {[k for k in response_data if 'switch' in k.lower() or 'agent' in k.lower()]}"
                )
            else:
                logger.info("No agent switch detected in response data")

    def _convert_confidence_score(self, confidence_value) -> str | None:
        """Convert confidence score to categorical value (high/medium/low)."""
        if confidence_value is None:
            return None

        # If already categorical, return as-is
        if isinstance(confidence_value, str) and confidence_value in [
            "high",
            "medium",
            "low",
        ]:
            return confidence_value

        # Convert numeric to categorical
        if isinstance(confidence_value, (int, float)):
            if confidence_value >= 0.8:
                return "high"
            elif confidence_value >= 0.5:
                return "medium"
            else:
                return "low"

        # Fallback to medium for unknown values
        return "medium"

    async def _stream_answer(self, chat_response, separators: list[str]):
        """Helper to stream answer and buffer JSON part."""
        state = {
            "buffer": "",
            "json_part_started": False,
            "log_buffer": "",
            "max_sep_len": max((len(s) for s in separators), default=0),
        }

        async for chunk in chat_response:
            if not self._is_valid_chunk(chunk):
                continue

            state["log_buffer"] += chunk.content

            if state["json_part_started"]:
                state["buffer"] += chunk.content
                continue

            # Process chunk for separator detection
            for result in self._process_chunk_for_separator(chunk.content, separators, state):
                yield result

        logger.info(f"Agent log: {state['log_buffer']}")
        yield ("final", state["buffer"], state["json_part_started"])

    def _is_valid_chunk(self, chunk) -> bool:
        """Check if chunk has valid content."""
        return hasattr(chunk, "content") and chunk.content

    def _process_chunk_for_separator(self, content: str, separators: list[str], state: dict):
        """Process chunk content looking for separators."""
        state["buffer"] += content

        separator_hit = self._find_earliest_separator(state["buffer"], separators)

        if separator_hit:
            for result in self._handle_separator_found(separator_hit, state):
                yield result
        else:
            for result in self._handle_no_separator(state):
                yield result

    def _find_earliest_separator(self, buffer: str, separators: list[str]):
        """Find the earliest occurring separator in buffer."""
        candidates = [(sep, buffer.find(sep)) for sep in separators]
        valid_candidates = [(sep, idx) for sep, idx in candidates if idx != -1]

        return min(valid_candidates, key=lambda t: t[1]) if valid_candidates else None

    def _handle_separator_found(self, separator_hit, state: dict):
        """Handle when a separator is found."""
        sep, idx = separator_hit
        answer_part = state["buffer"][:idx]
        json_part = state["buffer"][idx + len(sep) :]

        if answer_part:
            yield ("answer", answer_part)

        state["json_part_started"] = True
        state["buffer"] = json_part

    def _handle_no_separator(self, state: dict):
        """Handle streaming when no separator is found."""
        safe_len = len(state["buffer"]) - (state["max_sep_len"] - 1)

        if safe_len > 0:
            to_send = state["buffer"][:safe_len]
            state["buffer"] = state["buffer"][safe_len:]
            yield ("answer", to_send)

    async def run_stream(self, message_list: list[ChatMessage]) -> AsyncIterator[Frame]:
        """Run with enhanced JSON streaming and transparency support."""
        logger.info("Starting enhanced workflow execution with transparency")

        # Setup
        processed_messages = prepare_messages(message_list)
        message_id = str(uuid.uuid4())
        separators = ["⟦AEP:1⟧"]

        # Initialize capabilities and frame queue
        frame_queue = []
        self.capabilities = self._setup_capabilities(message_id, frame_queue)

        try:
            # Initial setup and progress
            async for frame in self._run_initial_setup(frame_queue):
                yield frame

            # Execute main agent workflow
            chat_response = await self.general_agent.arun(messages=processed_messages, stream=True)

            # Process streaming response
            async for frame in self._process_streaming_response(
                chat_response, separators, frame_queue
            ):
                yield frame

        except Exception as e:
            logger.error(f"Error in enhanced run_workflow: {e}")
        finally:
            # Emit remaining frames and finalize
            for frame in self._finalize_stream(frame_queue):
                yield frame

    def _setup_capabilities(self, message_id: str, frame_queue: list):
        """Setup agent capabilities with frame emitter."""
        capabilities = AgentCapabilities(message_id)

        def emit_frame(frame):
            frame_queue.append(frame)

        capabilities.set_frame_emitter(emit_frame)
        return capabilities

    async def _run_initial_setup(self, frame_queue: list):
        """Run initial setup and emit progress frames."""
        # Initial progress signal
        await self.capabilities.update_progress(
            "Initializing agent...", progress=0.05, stage="init"
        )

        # Emit queued frames
        while frame_queue:
            yield frame_queue.pop(0)

        # Show thinking process
        await self.capabilities.think_aloud(
            "Analyzing your request and preparing response...", stage="analysis"
        )

        # Emit thinking frames
        while frame_queue:
            yield frame_queue.pop(0)

    async def _process_streaming_response(self, chat_response, separators: list, frame_queue: list):
        """Process the streaming response from the agent."""
        buffer = ""
        json_part_started = False

        async for result in self._stream_answer(chat_response, separators):
            # Emit any queued frames first
            while frame_queue:
                yield frame_queue.pop(0)

            if result[0] == "answer":
                if result[1]:
                    yield AnswerDelta(content=result[1])
            elif result[0] == "final":
                buffer = result[1]
                json_part_started = result[2]

        # Handle remaining content and metadata
        async for frame in self._handle_final_content(buffer, json_part_started, frame_queue):
            yield frame

    async def _handle_final_content(self, buffer: str, json_part_started: bool, frame_queue: list):
        """Handle final content and metadata processing."""
        if not json_part_started and buffer:
            yield AnswerDelta(content=buffer)

        if json_part_started and buffer:
            # Progress: parsing metadata
            await self.capabilities.update_progress(
                "Processing additional insights...", progress=0.9, stage="postprocess"
            )

            # Emit progress frame
            while frame_queue:
                yield frame_queue.pop(0)

            for frame in self._parse_json_metadata(buffer):
                yield frame

    def _finalize_stream(self, frame_queue: list):
        """Finalize the stream by emitting remaining frames."""
        while frame_queue:
            yield frame_queue.pop(0)

        yield Finalize(reason="done")
