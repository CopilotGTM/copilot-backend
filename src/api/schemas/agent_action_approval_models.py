from __future__ import annotations

from pydantic import BaseModel, Field


class AgentActionApprovalPreferenceItem(BaseModel):
    """One effective preference row shown in the user settings UI."""

    tool_name: str
    label: str
    category: str
    approval_required: bool
    default_approval_required: bool
    editable: bool
    locked_reason: str | None = None


class AgentActionApprovalPreferencesResponse(BaseModel):
    """Versioned settings payload for agent action approval preferences."""

    schema_version: int = 1
    tools: list[AgentActionApprovalPreferenceItem]


class UpdateAgentActionApprovalPreferencesRequest(BaseModel):
    """Partial update map keyed by tool name for editable approval toggles."""

    tools: dict[str, bool] = Field(default_factory=dict)
