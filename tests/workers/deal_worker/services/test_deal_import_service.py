"""Tests for DealImportProcessingService payload building."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.service.crm.internal.deal_write_service import DealWriteServiceError
from src.workers.deal_worker.services.deal_import_service import (
    DealImportContext,
    DealImportProcessingService,
)


class DummyPublisher:
    def publish_progress(self, *_args, **_kwargs) -> None:
        return None


def _service() -> DealImportProcessingService:
    return DealImportProcessingService(
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
        storage_path="crm_imports/deals/test.csv",
        publisher=DummyPublisher(),
    )


def _context(pipeline, stage=None) -> DealImportContext:
    pipeline_by_name = {pipeline.name.lower(): pipeline}
    stages_by_pipeline = {}
    if stage is not None:
        stages_by_pipeline[pipeline.pipeline_id] = {stage.name.lower(): stage}
    return DealImportContext(
        integration_id=uuid4(),
        pipeline_by_name=pipeline_by_name,
        stages_by_pipeline=stages_by_pipeline,
        default_pipeline=pipeline,
    )


def test_build_payload_uses_company_fallback_and_default_pipeline() -> None:
    service = _service()
    pipeline = SimpleNamespace(pipeline_id=uuid4(), name="Default Pipeline", label=None)
    context = _context(pipeline)

    row = {
        "deal_name": "",
        "company_name": "Acme Corp",
        "company_domain": "acme.com",
    }

    payload, warnings = service._build_payload(row, context, user_map={})

    assert payload["name"] == "Acme Corp"
    assert payload["pipeline_id"] == pipeline.pipeline_id
    assert payload["stage_id"] is None
    assert warnings == []


def test_build_payload_owner_email_missing_raises() -> None:
    service = _service()
    pipeline = SimpleNamespace(pipeline_id=uuid4(), name="Pipeline", label=None)
    context = _context(pipeline)

    row = {"deal_name": "Deal 1", "owner_email": "missing@acme.com"}

    with pytest.raises(DealWriteServiceError) as exc:
        service._build_payload(row, context, user_map={})

    assert "Owner not found" in exc.value.message


def test_build_payload_stage_not_found_raises() -> None:
    service = _service()
    pipeline = SimpleNamespace(pipeline_id=uuid4(), name="Pipeline", label=None)
    context = _context(pipeline, stage=None)

    row = {
        "deal_name": "Deal 2",
        "pipeline_name": "Pipeline",
        "stage_name": "Qualification",
    }

    with pytest.raises(DealWriteServiceError) as exc:
        service._build_payload(row, context, user_map={})

    assert "Stage not found" in exc.value.message


def test_build_payload_team_member_warning() -> None:
    service = _service()
    pipeline = SimpleNamespace(pipeline_id=uuid4(), name="Pipeline", label=None)
    context = _context(pipeline)

    user_id = uuid4()
    user_map = {"owner@acme.com": user_id}
    row = {
        "deal_name": "Deal 3",
        "team_member_emails": "owner@acme.com,missing@acme.com",
    }

    payload, warnings = service._build_payload(row, context, user_map=user_map)

    assert payload["team_members"] == [{"user_id": user_id, "role": "member"}]
    assert warnings == [{"email": "missing@acme.com", "warning": "team_member_not_found"}]


def test_build_payload_invalid_close_date_raises() -> None:
    service = _service()
    pipeline = SimpleNamespace(pipeline_id=uuid4(), name="Pipeline", label=None)
    context = _context(pipeline)

    row = {"deal_name": "Deal 4", "close_date": "not-a-date"}

    with pytest.raises(DealWriteServiceError) as exc:
        service._build_payload(row, context, user_map={})

    assert "Invalid close_date" in exc.value.message
