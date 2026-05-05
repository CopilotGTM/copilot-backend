"""API handler tests for authenticated lead-form management endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import Response as FastAPIResponse
from src.api.routers.crm.leads.lead_forms_handler import create_lead_form
from src.api.schemas.lead_form_models import CreateLeadFormRequest, LeadFormFieldRequest
from src.domain.models.master_tables import (
    PublicIngressKey,
    PublicIngressKeyStatus,
    PublicIngressKeyType,
    PublicIngressResourceType,
)
from src.domain.models.organisation_tables.sync.crm.lead_forms import (
    LeadFormFieldDataType,
    LeadFormFieldTargetType,
    LeadFormStatus,
)


@pytest.mark.asyncio
async def test_create_lead_form_calls_service_instead_of_placeholder() -> None:
    """Authenticated form creation should create the form instead of returning the old 403."""
    org_id = uuid4()
    form = SimpleNamespace(
        form_id=uuid4(),
        name="Website demo",
        description=None,
        status=LeadFormStatus.ACTIVE,
        current_version=1,
        settings={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    field = SimpleNamespace(
        field_id=uuid4(),
        form_id=form.form_id,
        external_key="email",
        label="Email",
        target_type=LeadFormFieldTargetType.LEAD_FIELD,
        target_key="email",
        data_type=LeadFormFieldDataType.EMAIL,
        required=True,
        display_order=0,
        default_value=None,
        validation={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    ingress_key = PublicIngressKey(
        organisation_id=org_id,
        key_type=PublicIngressKeyType.PUBLIC_FORM,
        resource_type=PublicIngressResourceType.LEAD_FORM,
        resource_id=form.form_id,
        public_key="lf_test",
        status=PublicIngressKeyStatus.ACTIVE,
    )

    class Service:
        async def create_form(self, body):
            return form, [field], ingress_key

    response = FastAPIResponse()

    result = await create_lead_form(
        body=CreateLeadFormRequest(
            name="Website demo",
            status=LeadFormStatus.ACTIVE,
            fields=[
                LeadFormFieldRequest(
                    external_key="email",
                    label="Email",
                    target_type=LeadFormFieldTargetType.LEAD_FIELD,
                    target_key="email",
                    data_type=LeadFormFieldDataType.EMAIL,
                    required=True,
                )
            ],
        ),
        request=SimpleNamespace(base_url="https://api.example.com/"),  # type: ignore[arg-type]
        response=response,
        service=Service(),  # type: ignore[arg-type]
        _rl_write=None,
    )

    assert response.status_code == 201
    assert result.data.public_form_key == "lf_test"
    assert result.data.public_form_url == "https://api.example.com/public/v1/forms/lf_test"
