from __future__ import annotations

from uuid import uuid4

from src.service.crm.mutations.policies import DealMutationPolicy


def test_deal_create_semantics_are_creation_only() -> None:
    changed_fields = {
        "stage_id": {"old": None, "new": str(uuid4())},
        "owner_id": {"old": None, "new": str(uuid4())},
        "amount": {"old": None, "new": 1000},
    }

    assert DealMutationPolicy.semantic_for_event("crm.deal.created", changed_fields) == [
        "crm.deal.created"
    ]


def test_deal_update_semantics_are_curated() -> None:
    changed_fields = {
        "amount": {"old": 1000, "new": 2000},
        "priority": {"old": "medium", "new": "high"},
        "description": {"old": "old", "new": "new"},
        "custom_properties.procurement_stage": {"old": "legal", "new": "security"},
    }

    assert DealMutationPolicy.semantic_for_event("crm.deal.updated", changed_fields) == [
        "crm.deal.amount_changed"
    ]


def test_stage_and_owner_actions_have_specific_semantics() -> None:
    assert DealMutationPolicy.semantic_for_event(
        "crm.deal.stage_changed",
        {"stage_id": {"old": "a", "new": "b"}},
    ) == ["crm.deal.stage_changed"]

    assert DealMutationPolicy.semantic_for_event(
        "crm.deal.owner_changed",
        {"owner_name": {"old": "Old", "new": "New"}},
    ) == ["crm.deal.owner_changed"]


def test_lifecycle_action_semantics_include_stage_only_when_stage_changed() -> None:
    assert DealMutationPolicy.semantic_for_event(
        "crm.deal.closed",
        {
            "deal_status": {"old": "open", "new": "won"},
            "stage_id": {"old": "proposal", "new": "won"},
        },
    ) == ["crm.deal.closed", "crm.deal.stage_changed"]

    assert DealMutationPolicy.semantic_for_event(
        "crm.deal.reopened",
        {"deal_status": {"old": "won", "new": "open"}},
    ) == ["crm.deal.reopened"]
