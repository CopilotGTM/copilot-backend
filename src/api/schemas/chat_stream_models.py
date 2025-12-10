from __future__ import annotations

import time
from typing import Any, Literal, Union

from pydantic import BaseModel, Field


class Suggestion(BaseModel):
    label: str
    payload: dict[str, Any] | None = None


class Citation(BaseModel):
    label: str | None = None
    url: str | None = None
    snippet: str | None = None


class FieldSingleSelect(BaseModel):
    name: str
    kind: Literal["single_select"] = "single_select"
    options: list[str]
    required: bool | None = False


FieldSpec = FieldSingleSelect


class FrameBase(BaseModel):
    type: str
    # Production-level metadata
    job_id: str = Field(
        default="", description="Job ID - identifier for tracking the processing job"
    )
    assistant_message_id: str = Field(
        default="",
        description="Assistant Message ID - identifier for the assistant's message being created",
    )
    version: str = Field(default="1.0", description="Frame schema version")
    timestamp: float = Field(
        default_factory=time.time, description="Unix timestamp when frame was created"
    )
    sequence: int | None = Field(default=None, description="Optional sequence number for ordering")


class AnswerDelta(FrameBase):
    type: Literal["answer.delta"] = "answer.delta"
    content: str
    format: Literal["markdown", "plain"] | None = None


class AnswerReplace(FrameBase):
    type: Literal["answer.replace"] = "answer.replace"
    content: str
    format: Literal["markdown", "plain"] | None = None


class AnswerMetrics(FrameBase):
    type: Literal["answer.metrics"] = "answer.metrics"
    confidence: Literal["high", "medium", "low", "neutral"] | None = None


class SuggestionsReplace(FrameBase):
    type: Literal["suggestions.replace"] = "suggestions.replace"
    # Accept either plain strings or Suggestion objects
    items: list[str | Suggestion]


class SourcesReplace(FrameBase):
    # Canonical list of sources/citations referenced in the answer
    type: Literal["sources.replace"] = "sources.replace"
    # Keep key name as `sources` to match existing worker expectations
    sources: list[Citation] | None = None


class ProgressUpdate(FrameBase):
    # User-visible progress or status updates during long operations
    type: Literal["progress.update"] = "progress.update"
    message: str
    stage: str | None = None
    progress: float | None = Field(default=None, ge=0.0, le=1.0)
    eta_seconds: int | None = Field(default=None, ge=0)


class AgentSwitch(FrameBase):
    # Signal intent to switch to a different agent capability
    type: Literal["agent.switch"] = "agent.switch"
    from_agent: str | None = None
    to_agent: str
    reason: str | None = None


class ToolUsage(FrameBase):
    # Track tool usage for transparency
    type: Literal["tool.usage"] = "tool.usage"
    tool_name: str
    tool_description: str | None = None
    status: Literal["starting", "in_progress", "completed", "failed"] = "starting"
    parameters: dict[str, Any] | None = None
    result_summary: str | None = None
    duration_ms: int | None = None


class ThinkingUpdate(FrameBase):
    # Show agent thinking/reasoning process
    type: Literal["thinking.update"] = "thinking.update"
    thought: str
    stage: str | None = None


class QuestionPrompt(FrameBase):
    type: Literal["question.prompt"] = "question.prompt"
    prompt: str
    fields: list[FieldSpec] | None = None
    await_user_response: bool = True


class ActionCall(FrameBase):
    type: Literal["action.call"] = "action.call"
    action: str
    args: dict[str, Any] | None = None
    call_id: str


class ActionResult(FrameBase):
    type: Literal["action.result"] = "action.result"
    call_id: str
    ok: bool
    data: Any | None = None
    error: dict[str, Any] | None = None


class Finalize(FrameBase):
    type: Literal["finalize"] = "finalize"
    reason: Literal["done", "aborted", "error"] | None = None


class ErrorFrame(FrameBase):
    type: Literal["error"] = "error"
    code: str
    message: str
    debug: Any | None = None


class LogFrame(FrameBase):
    type: Literal["log"] = "log"
    level: Literal["info", "warn", "debug"]
    message: str


Frame = Union[
    AnswerDelta,
    AnswerReplace,
    AnswerMetrics,
    SuggestionsReplace,
    SourcesReplace,
    ProgressUpdate,
    AgentSwitch,
    ToolUsage,
    ThinkingUpdate,
    QuestionPrompt,
    ActionCall,
    ActionResult,
    Finalize,
    ErrorFrame,
    LogFrame,
]
