"""Contracts for the organisation-scoped Kai Training profile."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

KaiTrainingFieldName = Literal[
    "about_company",
    "what_you_sell",
    "who_you_sell_to",
    "why_customers_choose_you",
    "instructions",
]


class KaiTrainingFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    about_company: str = Field(default="", max_length=2000)
    what_you_sell: str = Field(default="", max_length=2000)
    who_you_sell_to: str = Field(default="", max_length=2000)
    why_customers_choose_you: str = Field(default="", max_length=2000)
    instructions: str = Field(default="", max_length=2000)


class UpdateKaiTrainingRequest(BaseModel):
    """Partial user overrides; omitted fields retain their current provenance."""

    model_config = ConfigDict(extra="forbid")

    about_company: str | None = Field(default=None, max_length=2000)
    what_you_sell: str | None = Field(default=None, max_length=2000)
    who_you_sell_to: str | None = Field(default=None, max_length=2000)
    why_customers_choose_you: str | None = Field(default=None, max_length=2000)
    instructions: str | None = Field(default=None, max_length=2000)


class KaiTrainingSource(BaseModel):
    label: str = Field(max_length=500)
    url: str = Field(max_length=2000)


class KaiTrainingResponse(BaseModel):
    organisation_name: str
    website: str | None = None
    fields: KaiTrainingFields
    field_origins: dict[KaiTrainingFieldName, Literal["research", "user", "empty"]]
    sources: list[KaiTrainingSource] = Field(default_factory=list)
    research_status: Literal[
        "not_started", "pending", "in_progress", "completed", "failed"
    ] = "not_started"
    last_researched_at: datetime | None = None
    research_error: str | None = None
    can_edit: bool
