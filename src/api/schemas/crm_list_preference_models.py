"""Contracts for persisting one CRM collection's sorting preference."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

CRMListEntity = Literal["contacts", "accounts", "deals", "tasks"]
SortOrder = Literal["asc", "desc"]


class CRMListSortingPreferences(BaseModel):
    """Canonical sorting returned after a CRM list preference update."""

    model_config = ConfigDict(extra="forbid")

    sort_by: str
    sort_order: SortOrder


class UpdateCRMListSortingRequest(CRMListSortingPreferences):
    """Complete sorting selection saved after a collection header click."""
