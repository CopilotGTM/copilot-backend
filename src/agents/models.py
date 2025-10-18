from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChatAgentSource(BaseModel):
    name: str | None = None
    url: str | None = None
    mimetype: str | None = None
    snippet: str | None = None


class ChatAgentOutput(BaseModel):
    answer: str = Field(default="")
    confidence_score: Literal["high", "medium", "low"] | None = None
    sources: list[ChatAgentSource] | None = Field(default=None)
    suggested_questions: list[str] | None = Field(default=None)
