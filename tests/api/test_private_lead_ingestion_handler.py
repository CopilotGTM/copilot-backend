"""Tests for private lead-ingestion endpoint authentication and validation errors."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import Response as FastAPIResponse
from fastapi.responses import JSONResponse
from src.api.routers.crm.leads import private_lead_ingestion_handler
from src.api.routers.crm.leads.private_lead_ingestion_handler import (
    ingest_private_form_submission,
    ingest_private_lead,
)
from src.domain.models.master_tables import PublicIngressResourceType
from src.service.crm.access.private_lead_ingestion_auth_service import (
    PrivateLeadIngestionAuthService,
)


@pytest.mark.asyncio
async def test_private_lead_ingestion_rejects_unknown_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return 422 when request body contains any unknown fields."""

    async def _authenticate(self, *, key_id, authorization, client_ip):
        return SimpleNamespace(
            organisation_id=uuid4(),
            public_key="skid_test",
            key_id=uuid4(),
        )

    monkeypatch.setattr(PrivateLeadIngestionAuthService, "authenticate", _authenticate)

    async def _acquire_master_session():
        yield object()

    monkeypatch.setattr(
        private_lead_ingestion_handler,
        "acquire_master_async_session",
        _acquire_master_session,
    )

    response = await ingest_private_lead(
        body={
            "email": "buyer@example.com",
            "unexpected": "field",
        },
        request=SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.10")),  # type: ignore[arg-type]
        response=FastAPIResponse(),
        key_id="skid_test",
        authorization="Bearer cgtm_live_secret",
        idempotency_key="idem-1",
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 422
    assert json.loads(response.body)["error"]["code"] == "invalid_payload"

    missing_key_response = await ingest_private_lead(
        body={"email": "buyer@example.com"},
        request=SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.10")),  # type: ignore[arg-type]
        response=FastAPIResponse(),
    )

    assert isinstance(missing_key_response, JSONResponse)
    assert missing_key_response.status_code == 401
    assert json.loads(missing_key_response.body)["error"]["code"] == "missing_key_id"


@pytest.mark.asyncio
async def test_private_form_submission_uses_form_scoped_key_and_requires_idempotency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mapped private form submissions authenticate lead_form keys before form mapping."""
    auth_calls = []

    async def _authenticate(self, *, key_id, authorization, client_ip, resource_type):
        auth_calls.append(resource_type)
        return SimpleNamespace(
            organisation_id=uuid4(),
            resource_id=uuid4(),
            public_key="skid_form",
            key_id=uuid4(),
        )

    monkeypatch.setattr(PrivateLeadIngestionAuthService, "authenticate", _authenticate)

    async def _acquire_master_session():
        yield object()

    monkeypatch.setattr(
        private_lead_ingestion_handler,
        "acquire_master_async_session",
        _acquire_master_session,
    )

    response = await ingest_private_form_submission(
        body={"fields": {"email": "buyer@example.com"}, "context": {}},
        request=SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.10")),  # type: ignore[arg-type]
        response=FastAPIResponse(),
        key_id="skid_form",
        authorization="Bearer cgtm_live_secret",
        idempotency_key=None,
    )

    assert auth_calls == [PublicIngressResourceType.LEAD_FORM]
    assert isinstance(response, JSONResponse)
    assert response.status_code == 400
    assert json.loads(response.body)["error"]["code"] == "missing_idempotency_key"
