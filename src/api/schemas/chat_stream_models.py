from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field


class Suggestion(BaseModel):
    label: str
    payload: Optional[Dict[str, Any]] = None


class Citation(BaseModel):
    label: Optional[str] = None
    url: Optional[str] = None
    snippet: Optional[str] = None


class FieldText(BaseModel):
    name: str
    kind: Literal["text"] = "text"
    required: Optional[bool] = False
    placeholder: Optional[str] = None


class FieldSingleSelect(BaseModel):
    name: str
    kind: Literal["single_select"] = "single_select"
    options: List[str]
    required: Optional[bool] = False


class FieldNumber(BaseModel):
    name: str
    kind: Literal["number"] = "number"
    min: Optional[float] = None
    max: Optional[float] = None
    required: Optional[bool] = False


FieldSpec = Union[FieldText, FieldSingleSelect, FieldNumber]


class FrameBase(BaseModel):
    type: str


class AnswerDelta(FrameBase):
    type: Literal["answer.delta"] = "answer.delta"
    message_id: str
    content: str
    format: Optional[Literal["markdown", "plain"]] = None


class AnswerReplace(FrameBase):
    type: Literal["answer.replace"] = "answer.replace"
    message_id: str
    content: str
    format: Optional[Literal["markdown", "plain"]] = None


class AnswerMetrics(FrameBase):
    type: Literal["answer.metrics"] = "answer.metrics"
    message_id: str
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    citations: Optional[List[Citation]] = None


class SuggestionsReplace(FrameBase):
    type: Literal["suggestions.replace"] = "suggestions.replace"
    message_id: str
    items: List[Suggestion]


class QuestionPrompt(FrameBase):
    type: Literal["question.prompt"] = "question.prompt"
    message_id: str
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
    message_id: str
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
    QuestionPrompt,
    ActionCall,
    ActionResult,
    Finalize,
    ErrorFrame,
    LogFrame,
]

