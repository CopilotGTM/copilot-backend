"""Schemas for the application-provided Sales Dashboard."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class SalesDashboardDateRange(str, Enum):
    LAST_30_DAYS = "last_30_days"
    LAST_90_DAYS = "last_90_days"
    THIS_QUARTER = "this_quarter"
    YEAR_TO_DATE = "year_to_date"


class SalesDashboardPrimarySegment(str, Enum):
    STAGE = "stage"
    PIPELINE = "pipeline"
    STATUS = "status"
    OWNER = "owner"


class SalesDashboardQueryParams(BaseModel):
    date_range: SalesDashboardDateRange | None = None
    primary_segment: SalesDashboardPrimarySegment = SalesDashboardPrimarySegment.STAGE
    from_date: datetime | None = None
    to_date: datetime | None = None
    min_deal_value: float | None = Field(None, ge=0)
    max_deal_value: float | None = Field(None, ge=0)
    pipeline_ids: list[UUID] = Field(default_factory=list)
    owner_ids: list[UUID] = Field(default_factory=list)
    include_widgets: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_date_bounds(self) -> SalesDashboardQueryParams:
        if (
            self.from_date is not None
            and self.to_date is not None
            and self.from_date > self.to_date
        ):
            raise ValueError("from_date must be before or equal to to_date")
        if (
            self.min_deal_value is not None
            and self.max_deal_value is not None
            and self.min_deal_value > self.max_deal_value
        ):
            raise ValueError("min_deal_value must be less than or equal to max_deal_value")
        return self


class SalesDashboardFilterSet(BaseModel):
    date_range: SalesDashboardDateRange
    primary_segment: SalesDashboardPrimarySegment = SalesDashboardPrimarySegment.STAGE
    from_date: datetime
    to_date: datetime
    min_deal_value: float | None = None
    max_deal_value: float | None = None
    pipeline_ids: list[UUID] = Field(default_factory=list)
    owner_ids: list[UUID] = Field(default_factory=list)


class WidgetEmptyState(BaseModel):
    reason: str
    message: str


class SalesDashboardWidget(BaseModel):
    widget_id: str
    widget_type: str
    title: str
    data: dict[str, Any] | list[dict[str, Any]] | None
    empty_state: WidgetEmptyState | None = None
    warnings: list[str] = Field(default_factory=list)


class SalesDashboardSection(BaseModel):
    section_id: str
    title: str
    widgets: list[SalesDashboardWidget]


class SalesDashboardResponse(BaseModel):
    dashboard_id: str = "sales"
    schema_version: int = 1
    title: str = "Sales Dashboard"
    generated_at: datetime
    effective_filters: dict[str, Any]
    sections: list[SalesDashboardSection]
    warnings: list[str] = Field(default_factory=list)


class SalesDashboardPreferences(BaseModel):
    filters: dict[str, Any] = Field(default_factory=dict)
    layout_density: str = "comfortable"
    collapsed_widget_ids: list[str] = Field(default_factory=list)
    hidden_widget_ids: list[str] = Field(default_factory=list)


class UpdateSalesDashboardPreferencesRequest(BaseModel):
    filters: dict[str, Any] = Field(default_factory=dict)
    layout_density: str = "comfortable"
    collapsed_widget_ids: list[str] = Field(default_factory=list)
    hidden_widget_ids: list[str] = Field(default_factory=list)
