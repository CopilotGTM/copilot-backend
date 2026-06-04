"""Tests for CRM task API schemas."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError
from src.api.schemas.crm_task_models import CreateTaskRequest, UpdateTaskRequest
from src.domain.models.organisation_tables.sync.crm import CRMActivityObjectType


def test_create_task_accepts_lead_related_object_pair() -> None:
    payload = CreateTaskRequest(
        subject="Follow up",
        related_object_type=CRMActivityObjectType.LEAD,
        related_local_id=uuid4(),
    )

    assert payload.related_object_type == CRMActivityObjectType.LEAD


def test_create_task_rejects_partial_related_object_pair() -> None:
    with pytest.raises(ValidationError):
        CreateTaskRequest(
            subject="Follow up",
            related_object_type=CRMActivityObjectType.CONTACT,
        )


def test_create_task_rejects_non_deal_related_object_with_deal_id() -> None:
    with pytest.raises(ValidationError):
        CreateTaskRequest(
            subject="Follow up",
            deal_id=uuid4(),
            related_object_type=CRMActivityObjectType.CONTACT,
            related_local_id=uuid4(),
        )


def test_create_task_rejects_mismatched_deal_id_and_deal_related_id() -> None:
    with pytest.raises(ValidationError):
        CreateTaskRequest(
            subject="Follow up",
            deal_id=uuid4(),
            related_object_type=CRMActivityObjectType.DEAL,
            related_local_id=uuid4(),
        )


def test_update_task_rejects_non_deal_related_object_with_deal_id() -> None:
    with pytest.raises(ValidationError):
        UpdateTaskRequest(
            deal_id=uuid4(),
            related_object_type=CRMActivityObjectType.CONTACT,
            related_local_id=uuid4(),
        )


def test_update_task_accepts_matching_deal_related_pair() -> None:
    deal_id = uuid4()

    payload = UpdateTaskRequest(
        deal_id=deal_id,
        related_object_type=CRMActivityObjectType.DEAL,
        related_local_id=deal_id,
    )

    assert payload.related_local_id == deal_id
