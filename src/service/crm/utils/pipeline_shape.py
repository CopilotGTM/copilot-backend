"""Shared CRM pipeline shape rules.

Deal creation, pipeline mutation, import setup, and provider snapshot writers
all need the same definition of "usable": a pipeline must be able to represent
active, won, and lost deal states.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.domain.models.organisation_tables.sync.crm.deals import CRMStageState

REQUIRED_INTERNAL_PIPELINE_STATES = {
    CRMStageState.OPEN.value,
    CRMStageState.WON.value,
    CRMStageState.LOST.value,
}


def stage_state_value(stage: object) -> str | None:
    """Return a stage's lifecycle state as the canonical stored string.

    Callers pass both ORM rows and API input models here, so this function accepts
    plain objects and normalizes enum-backed and string-backed stage states.
    """
    value = getattr(stage, "stage_state", None)
    if isinstance(value, CRMStageState):
        return value.value
    return str(value) if value is not None else None


def missing_required_pipeline_states(stages: Iterable[object]) -> set[str]:
    """Report which lifecycle states a pipeline cannot currently represent.

    The rule is intentionally state-based rather than count-based: one open,
    one won, and one lost stage is enough for internal deal creation and close
    actions, regardless of how many extra provider-specific stages exist.
    """
    present = {state for stage in stages if (state := stage_state_value(stage))}
    return REQUIRED_INTERNAL_PIPELINE_STATES - present


def has_required_pipeline_shape(stages: Iterable[object]) -> bool:
    """Return whether a stage collection can support internal deal writes."""
    return not missing_required_pipeline_states(stages)


def mark_pipeline_usability(pipeline: Any, stages: Iterable[object]) -> bool:
    """Persist the derived write gate on a pipeline-like object.

    Import writers and internal setup both call this before flush/commit so a
    pipeline's active flag and explicit metadata agree with its current stages.
    """
    usable = has_required_pipeline_shape(stages)
    # Keep both fields in sync during the rollout from generic is_active checks
    # to the explicit metadata flag used by internal deal-write callers.
    pipeline.is_active = usable
    properties = getattr(pipeline, "properties", None) or {}
    pipeline.properties = {**properties, "usable_for_internal_deals": usable}
    return usable


def required_pipeline_shape_message() -> str:
    """Return the shared user-facing error for incomplete pipeline shapes."""
    return "A usable internal CRM pipeline must have at least one open, won, and lost stage."
