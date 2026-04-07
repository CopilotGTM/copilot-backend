"""Pydantic models for StakeholderResearchAgent structured output."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class StakeholderResearchResult(BaseModel):
    """Structured output from StakeholderResearchAgent.

    The agent populates identity fields (for direct DB writes) plus a
    full markdown profile blob. The service layer persists these to the
    DealStakeholder row.
    """

    status: Literal["found", "not_found", "error"] = Field(
        ...,
        description=(
            "'found' if profile data was retrieved, "
            "'not_found' if no public profile exists, "
            "'error' if tools failed (API error, rate limit, etc.)"
        ),
    )
    error_detail: str | None = Field(
        None,
        description="Brief reason when status is 'error'. Omit otherwise.",
    )

    # Identity fields (populated when status == "found")
    name: str | None = Field(None, description="Full name")
    first_name: str | None = Field(None, description="First name")
    last_name: str | None = Field(None, description="Last name")
    job_title: str | None = Field(None, description="Current job title")
    linkedin_url: str | None = Field(None, description="LinkedIn profile URL")

    # Full markdown research profile — includes headline, location, background, insights
    research_markdown: str | None = Field(
        None,
        description=(
            "Complete markdown profile of the stakeholder when status='found'. "
            "Includes header (name, title, company, LinkedIn, location, headline), "
            "background (recent roles), and insights sections."
        ),
    )
