from __future__ import annotations

import time
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field


class Suggestion(BaseModel):
    label: str
    payload: Optional[Dict[str, Any]] = None


class Citation(BaseModel):
    label: Optional[str] = None
    url: Optional[str] = None
    snippet: Optional[str] = None


class FieldSingleSelect(BaseModel):
    name: str
    kind: Literal["single_select"] = "single_select"
    options: List[str]
    required: Optional[bool] = False


FieldSpec = FieldSingleSelect


class FrameBase(BaseModel):
    type: str
    # Production-level metadata
    frontend_id: str = Field(default="", description="Job ID - frontend identifier for request tracking")
    backend_id: str = Field(default="", description="Message ID - backend identifier for data correlation")
    version: str = Field(default="1.0", description="Frame schema version")
    timestamp: float = Field(default_factory=time.time, description="Unix timestamp when frame was created")
    sequence: Optional[int] = Field(default=None, description="Optional sequence number for ordering")


class AnswerDelta(FrameBase):
    type: Literal["answer.delta"] = "answer.delta"
    content: str
    format: Optional[Literal["markdown", "plain"]] = None


class AnswerReplace(FrameBase):
    type: Literal["answer.replace"] = "answer.replace"
    content: str
    format: Optional[Literal["markdown", "plain"]] = None


class AnswerMetrics(FrameBase):
    type: Literal["answer.metrics"] = "answer.metrics"
    confidence: Optional[Literal["high", "medium", "low"]] = None


class SuggestionsReplace(FrameBase):
    type: Literal["suggestions.replace"] = "suggestions.replace"
    # Accept either plain strings or Suggestion objects
    items: List[Union[str, Suggestion]]


class SourcesReplace(FrameBase):
    # Canonical list of sources/citations referenced in the answer
    type: Literal["sources.replace"] = "sources.replace"
    # Keep key name as `sources` to match existing worker expectations
    sources: Optional[List[Citation]] = None


class ProgressUpdate(FrameBase):
    # User-visible progress or status updates during long operations
    type: Literal["progress.update"] = "progress.update"
    message: str
    stage: Optional[str] = None
    progress: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    eta_seconds: Optional[int] = Field(default=None, ge=0)


class AgentSwitch(FrameBase):
    # Signal intent to switch to a different agent capability
    type: Literal["agent.switch"] = "agent.switch"
    from_agent: Optional[str] = None
    to_agent: str
    reason: Optional[str] = None


class ToolUsage(FrameBase):
    # Track tool usage for transparency
    type: Literal["tool.usage"] = "tool.usage" 
    tool_name: str
    tool_description: Optional[str] = None
    status: Literal["starting", "in_progress", "completed", "failed"] = "starting"
    parameters: Optional[Dict[str, Any]] = None
    result_summary: Optional[str] = None
    duration_ms: Optional[int] = None


class ThinkingUpdate(FrameBase):
    # Show agent thinking/reasoning process
    type: Literal["thinking.update"] = "thinking.update"
    thought: str
    stage: Optional[str] = None


class QuestionPrompt(FrameBase):
    type: Literal["question.prompt"] = "question.prompt"
    prompt: str
    fields: Optional[List[FieldSpec]] = None
    await_user_response: bool = True


class ActionCall(FrameBase):
    type: Literal["action.call"] = "action.call"
    action: str
    args: Optional[Dict[str, Any]] = None
    call_id: str


class ActionResult(FrameBase):
    type: Literal["action.result"] = "action.result"
    call_id: str
    ok: bool
    data: Optional[Any] = None
    error: Optional[Dict[str, Any]] = None


class Finalize(FrameBase):
    type: Literal["finalize"] = "finalize"
    reason: Optional[Literal["done", "aborted", "error"]] = None


class ErrorFrame(FrameBase):
    type: Literal["error"] = "error"
    code: str
    message: str
    debug: Optional[Any] = None


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

