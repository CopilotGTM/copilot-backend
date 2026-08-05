from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OnboardingStepId = Literal["google", "microsoft", "whatsapp"]


class OnboardingPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skipped_steps: list[OnboardingStepId] = Field(default_factory=list)
