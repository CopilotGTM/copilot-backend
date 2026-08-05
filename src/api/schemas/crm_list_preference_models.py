"""Contracts for persisting one CRM collection's sorting preference."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CRMListEntity = Literal["contacts", "accounts", "deals", "tasks"]
CRMColumnEntity = Literal["contacts", "accounts", "deals", "tasks", "leads"]
SortOrder = Literal["asc", "desc"]


class CRMListSortingPreferences(BaseModel):
    """Canonical sorting returned after a CRM list preference update."""

    model_config = ConfigDict(extra="forbid")

    sort_by: str
    sort_order: SortOrder


class UpdateCRMListSortingRequest(CRMListSortingPreferences):
    """Complete sorting selection saved after a collection header click."""


class CRMListColumn(BaseModel):
    """One backend-owned CRM list column and its current user state."""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    data_type: str
    source: Literal["standard", "custom"]
    value_path: str
    visible: bool
    default_visible: bool
    position: int
    sortable: bool
    hideable: bool


class CRMListConfiguration(BaseModel):
    """Page-ready column catalog plus the authenticated user's ordered selection."""

    model_config = ConfigDict(extra="forbid")

    entity: CRMColumnEntity
    columns: list[CRMListColumn]
    sort_by: str | None = None
    sort_order: SortOrder | None = None


class UpdateCRMListColumnsRequest(BaseModel):
    """Complete ordered visible column selection saved by one list page."""

    model_config = ConfigDict(extra="forbid")

    columns: list[str] = Field(min_length=1, max_length=50)
