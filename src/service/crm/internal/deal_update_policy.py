"""Policy engine for deciding how to handle each requested deal update.

This module centralizes the contract used by agent tools: it classifies updates
as applied, approval-gated, or skipped before any CRM write path runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

PolicyStatus = Literal["applied", "approval_required", "skipped"]

SAFE_DIRECT_FIELDS = {
    "amount",
    "currency",
    "description",
    "expected_close_date",
    "close_date",
    "priority",
    "probability",
    "forecast_category",
    "lead_source",
    "deal_type",
}
ACCOUNT_FIELDS = {"website", "domain", "linkedin_url", "industry"}
# TODO: Re-enable primary-contact updates when contact-write policy is designed.
# This module intentionally keeps primary-contact updates out of the first slice
# so we can build identity validation separately.
CONTACT_FIELDS: set[str] = set()
SENSITIVE_LIFECYCLE_FIELDS = {"stage_id", "outcome", "closed_reason", "reopen"}


@dataclass(frozen=True)
class DealUpdatePolicyDecision:
    """Decision details returned to all callers.

    `status` determines whether a write can proceed, while `reason` is consumed
    in tool responses to make approval behavior deterministic and explainable.
    """

    status: PolicyStatus
    requires_approval: bool
    reason: str


def classify_deal_update_policy(
    *,
    action: str,
    field: str | None,
    old_value: Any,
    new_value: Any,
    source: str,
    directly_requested: bool,
) -> DealUpdatePolicyDecision:
    """Classify whether a requested deal update can mutate CRM immediately.

    The policy is intentionally conservative: inferred changes usually require
    explicit user confirmation, while directly requested safe edits can apply in-line.
    """
    _ = old_value
    _ = source

    if new_value is None:
        return DealUpdatePolicyDecision(
            status="skipped",
            requires_approval=False,
            reason="No new value was provided for the update.",
        )

    if action == "update_deal_fields":
        # Safe scalar fields are in-scope for direct user requests.
        # Inferred field changes are held until confirmation.
        if field not in SAFE_DIRECT_FIELDS:
            return DealUpdatePolicyDecision(
                status="skipped",
                requires_approval=False,
                reason=f"Field '{field}' is not supported by the first deal-update slice.",
            )
        if directly_requested:
            return DealUpdatePolicyDecision(
                status="applied",
                requires_approval=False,
                reason="Safe deal field was directly requested by the user.",
            )
        return DealUpdatePolicyDecision(
            status="approval_required",
            requires_approval=True,
            reason="Deal field update was inferred from context and needs confirmation.",
        )

    if action == "update_deal_lifecycle":
        # Lifecycle transitions affect deal state and reporting; route inferred
        # outcomes through approval before changing state.
        if field in SENSITIVE_LIFECYCLE_FIELDS:
            return DealUpdatePolicyDecision(
                status="approval_required",
                requires_approval=True,
                reason="Stage changes inferred or lifecycle changes require confirmation.",
            )
        return DealUpdatePolicyDecision(
            status="skipped",
            requires_approval=False,
            reason=f"Lifecycle field '{field}' is not supported.",
        )

    if action == "update_deal_account":
        # Account-level data can reframe deal context, so inferred updates are
        # approval-gated by default.
        if field not in ACCOUNT_FIELDS:
            return DealUpdatePolicyDecision(
                status="skipped",
                requires_approval=False,
                reason=f"Account field '{field}' is not supported.",
            )
        if directly_requested:
            return DealUpdatePolicyDecision(
                status="applied",
                requires_approval=False,
                reason="Account field was directly requested by the user.",
            )
        return DealUpdatePolicyDecision(
            status="approval_required",
            requires_approval=True,
            reason="Account changes inferred from context require confirmation.",
        )

    if action == "update_deal_primary_contact":
        return DealUpdatePolicyDecision(
            status="skipped",
            requires_approval=False,
            reason="Primary contact updates are deferred from the first deal-update slice.",
        )

    return DealUpdatePolicyDecision(
        status="skipped",
        requires_approval=False,
        reason=f"Action '{action}' is not supported by the deal update policy.",
    )
