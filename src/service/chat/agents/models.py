from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ChatAgentSource(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    mimetype: Optional[str] = None
    snippet: Optional[str] = None


class ChatAgentOutput(BaseModel):
    answer: str = Field(default="")
    confidence_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    sources: Optional[List[ChatAgentSource]] = Field(default=None)
    suggested_questions: Optional[List[str]] = Field(default=None)

