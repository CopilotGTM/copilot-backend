"""Deal Memory model — persistent markdown memory document per deal."""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID, uuid4

from sqlalchemy import Column, Index, Integer, Text, UniqueConstraint
from sqlmodel import Field
from src.domain.models.organisation_tables.base_mixins import TimestampMixin


class DealMemory(TimestampMixin, table=True):
    """One markdown document per deal, incrementally updated.

    Stores the accumulated knowledge from meetings, emails, and notes.
    Used as pre-built context for downstream agents (DealSummary,
    RiskBlocker, Stakeholder, Briefing).
    """

    __tablename__: ClassVar[str] = "deal_memories"
    __table_args__: ClassVar[tuple] = (
        UniqueConstraint("organisation_id", "deal_id", name="uq_deal_memories_org_deal"),
        Index("ix_deal_memories_org", "organisation_id"),
    )

    memory_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(index=True)
    deal_id: UUID = Field(
        foreign_key="crm_deals.deal_id",
        index=True,
    )
    content: str = Field(
        sa_column=Column(Text, nullable=False),
    )
    version: int = Field(
        sa_column=Column(Integer, nullable=False, server_default="1"),
        default=1,
    )
    token_count: int | None = Field(default=None)


DealMemory.update_forward_refs()
