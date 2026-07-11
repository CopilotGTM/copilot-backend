"""Regression tests for lead-form service transaction ordering and payload guards.

The suite uses deterministic in-memory helpers to assert orchestration rules such as
public-key creation ordering, update-state transitions, and submission validation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from src.api.schemas.lead_form_models import (
    MAX_PUBLIC_SCALAR_STRING_LENGTH,
    MAX_PUBLIC_SUBMISSION_FIELDS,
    CreateLeadFormRequest,
    LeadFormFieldRequest,
    LeadFormItem,
    PublicLeadFormSubmitRequest,
    PublicLeadFormSubmitResponse,
    UpdateLeadFormRequest,
)
from src.domain.models.master_tables.organisation_models import Organisation
from src.domain.models.master_tables.public_ingress_key_models import PublicIngressKey
from src.domain.models.organisation_tables.sync.crm.custom_fields import CRMCustomFieldDataType
from src.domain.models.organisation_tables.sync.crm.lead_forms import (
    LeadForm,
    LeadFormField,
    LeadFormFieldDataType,
    LeadFormFieldTargetType,
    LeadFormStatus,
    LeadFormSubmission,
)
from src.domain.models.organisation_tables.sync.crm.leads import CRMLead
from src.service.crm.leads.form_management_service import (
    LeadFormsService,
    LeadFormsServiceError,
    PublicLeadFormSubmissionService,
)


class RecordingAsyncSession:
    """Lightweight session stub that records call order for assertion in tests."""

    def __init__(self, name: str, log: list[str], *, flush_error: Exception | None = None):
        self.name = name
        self.log = log
        self.added: list[object] = []
        self.flush_error = flush_error

    def add(self, instance: object) -> None:
        """Record which model type would be added to the session."""
        self.added.append(instance)
        self.log.append(f"{self.name}.add.{type(instance).__name__}")

    async def flush(self) -> None:
        """Record a flush call boundary so transaction ordering is observable."""
        self.log.append(f"{self.name}.flush")
        if self.flush_error:
            raise self.flush_error

    async def commit(self) -> None:
        """Record a commit call boundary to validate session ordering."""
        self.log.append(f"{self.name}.commit")

    async def rollback(self) -> None:
        """Record rollback for tests that expect write-path safety paths."""
        self.log.append(f"{self.name}.rollback")

    async def refresh(self, instance: object) -> None:
        """Record instance refresh calls and keep the assertions close to call order."""
        self.log.append(f"{self.name}.refresh.{type(instance).__name__}")

    async def close(self) -> None:
        """Record session disposal to mimic normal service cleanup."""
        self.log.append(f"{self.name}.close")


class _RowsResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> _RowsResult:
        return self

    def all(self) -> list[object]:
        return self._rows


def test_public_ingress_key_resolves_organisation_fk_in_public_schema() -> None:
    """Public ingress keys stay in the shared schema and reference public organisations."""
    assert Organisation.__table__.schema == "public"
    assert PublicIngressKey.__table__.schema == "public"

    organisation_fk = next(
        fk
        for fk in PublicIngressKey.__table__.c.organisation_id.foreign_keys
        if fk.parent.name == "organisation_id"
    )

    assert organisation_fk.column.table.schema == "public"
    assert organisation_fk.column.table.name == "organisations"
    assert organisation_fk.column.name == "organisation_id"


def test_lead_form_item_exposes_clickable_public_form_url() -> None:
    """Expose a public URL value from lead form item serialization."""
    item = LeadFormItem(
        form_id=uuid4(),
        public_form_key="lf_demo",
        public_form_url="https://api.example.com/public/v1/forms/lf_demo",
        name="Website demo",
        description=None,
        status=LeadFormStatus.ACTIVE,
        current_version=1,
        allowed_origins=[],
        settings={},
        fields=[],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    assert item.public_form_url == "https://api.example.com/public/v1/forms/lf_demo"


def test_public_submit_response_hides_internal_lead_id() -> None:
    """Submission responses must not expose internal lead ids in public payloads."""
    response = PublicLeadFormSubmitResponse(submission_id=uuid4())

    assert response.model_dump()["status"] == "accepted"
    assert "lead_id" not in response.model_dump()


def test_public_submit_request_limits_field_count() -> None:
    """Reject public submissions that exceed allowed public-field cardinality."""
    with pytest.raises(ValidationError):
        PublicLeadFormSubmitRequest(
            fields={f"field_{index}": index for index in range(MAX_PUBLIC_SUBMISSION_FIELDS + 1)}
        )


def test_public_submit_request_accepts_optional_idempotency_key() -> None:
    """Browser callers can provide an optional replay token for retry safety."""
    request = PublicLeadFormSubmitRequest(
        fields={"email": "buyer@example.com"},
        idempotency_key="public-form-123",
    )

    assert request.idempotency_key == "public-form-123"


def test_mapped_submission_payload_rejects_excess_total_bytes() -> None:
    """Reject many small values when the whole mapped-form payload is too large."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )

    with pytest.raises(LeadFormsServiceError) as exc_info:
        service._validate_payload_shape(
            {f"field_{index}": "x" * 2_000 for index in range(MAX_PUBLIC_SUBMISSION_FIELDS)}
        )

    assert exc_info.value.status_code == 413
    assert exc_info.value.data["code"] == "payload_too_large"


def test_mapped_submission_payload_rejects_deep_nested_json() -> None:
    """Reject deeply nested mapped-form values before recursive validation can grow unbounded."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )
    value = {"level": "leaf"}
    for _ in range(9):
        value = {"level": value}

    with pytest.raises(LeadFormsServiceError) as exc_info:
        service._validate_payload_shape({"metadata": value})

    assert exc_info.value.status_code == 413
    assert exc_info.value.data["code"] == "payload_too_deep"
    assert exc_info.value.data["field"].startswith("metadata.")


def test_mapped_submission_payload_rejects_large_nested_lists() -> None:
    """Reject a huge list under one submitted field even when top-level field count is low."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )

    with pytest.raises(LeadFormsServiceError) as exc_info:
        service._validate_payload_shape({"interests": ["demo"] * 201})

    assert exc_info.value.status_code == 413
    assert exc_info.value.data["code"] == "payload_too_large"
    assert exc_info.value.data["field"] == "interests"


def test_mapped_submission_payload_rejects_too_many_nested_nodes() -> None:
    """Reject nested JSON with too many nodes even when each scalar is small."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )

    with pytest.raises(LeadFormsServiceError) as exc_info:
        service._validate_payload_shape(
            {"metadata": {f"key_{index}": "x" for index in range(2001)}}
        )

    assert exc_info.value.status_code == 413
    assert exc_info.value.data["code"] == "payload_too_complex"


def test_form_create_rejects_incompatible_lead_field_type() -> None:
    """A configured lead column mapping should use a compatible submitted data type."""
    with pytest.raises(LeadFormsServiceError) as exc_info:
        LeadFormsService._validate_field_requests(
            [
                LeadFormFieldRequest(
                    external_key="budget",
                    label="Budget",
                    target_type=LeadFormFieldTargetType.LEAD_FIELD,
                    target_key="budget_amount",
                    data_type=LeadFormFieldDataType.TEXT,
                )
            ]
        )

    assert exc_info.value.status_code == 400
    assert "Unsupported data type" in exc_info.value.message


def test_form_create_rejects_required_ignore_field() -> None:
    """Admins should not be able to require a field that is discarded on submit."""
    with pytest.raises(LeadFormsServiceError) as exc_info:
        LeadFormsService._validate_field_requests(
            [
                LeadFormFieldRequest(
                    external_key="internal_note",
                    label="Internal note",
                    target_type=LeadFormFieldTargetType.IGNORE,
                    data_type=LeadFormFieldDataType.TEXT,
                    required=True,
                )
            ]
        )

    assert exc_info.value.status_code == 400
    assert "Ignored fields cannot be required" in exc_info.value.message


def test_form_create_rejects_duplicate_property_target_mapping() -> None:
    """Two external fields should not silently overwrite the same custom property."""
    with pytest.raises(LeadFormsServiceError) as exc_info:
        LeadFormsService._validate_field_requests(
            [
                LeadFormFieldRequest(
                    external_key="plan",
                    label="Plan",
                    target_type=LeadFormFieldTargetType.PROPERTY,
                    target_key="lead_plan",
                    data_type=LeadFormFieldDataType.TEXT,
                ),
                LeadFormFieldRequest(
                    external_key="selected_plan",
                    label="Selected plan",
                    target_type=LeadFormFieldTargetType.PROPERTY,
                    target_key=" lead_plan ",
                    data_type=LeadFormFieldDataType.TEXT,
                ),
            ]
        )

    assert exc_info.value.status_code == 400
    assert "Duplicate form target mapping" in exc_info.value.message


def test_form_create_rejects_duplicate_normalized_property_target_mapping() -> None:
    """Property mappings that normalize to one custom field should not double-write it."""
    with pytest.raises(LeadFormsServiceError) as exc_info:
        LeadFormsService._validate_field_requests(
            [
                LeadFormFieldRequest(
                    external_key="plan",
                    label="Plan",
                    target_type=LeadFormFieldTargetType.PROPERTY,
                    target_key="Lead.Plan",
                    data_type=LeadFormFieldDataType.TEXT,
                ),
                LeadFormFieldRequest(
                    external_key="selected_plan",
                    label="Selected plan",
                    target_type=LeadFormFieldTargetType.PROPERTY,
                    target_key="lead_plan",
                    data_type=LeadFormFieldDataType.TEXT,
                ),
            ]
        )

    assert exc_info.value.status_code == 400
    assert "Duplicate form target mapping" in exc_info.value.message


def test_form_create_allows_custom_prefixed_property_target_mapping() -> None:
    """Lead-form backfill does not strip custom_ prefixes, so these are distinct keys."""
    LeadFormsService._validate_field_requests(
        [
            LeadFormFieldRequest(
                external_key="plan",
                label="Plan",
                target_type=LeadFormFieldTargetType.PROPERTY,
                target_key="custom_plan",
                data_type=LeadFormFieldDataType.TEXT,
            ),
            LeadFormFieldRequest(
                external_key="selected_plan",
                label="Selected plan",
                target_type=LeadFormFieldTargetType.PROPERTY,
                target_key="plan",
                data_type=LeadFormFieldDataType.TEXT,
            ),
        ]
    )


def test_form_submission_rejects_invalid_email_values() -> None:
    """Email-typed form fields should reject malformed submitted email strings."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )
    field = LeadFormField(
        organisation_id=uuid4(),
        form_id=uuid4(),
        external_key="email",
        label="Email",
        target_type=LeadFormFieldTargetType.LEAD_FIELD,
        target_key="email",
        data_type=LeadFormFieldDataType.EMAIL,
        required=True,
    )

    with pytest.raises(LeadFormsServiceError) as exc_info:
        service._map_payload([field], {"email": "not-an-email"})

    assert exc_info.value.status_code == 422
    assert exc_info.value.data["field"] == "email"


def test_form_submission_coerces_date_and_datetime_values() -> None:
    """Date and datetime fields should store normalized ISO strings."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )
    form_id = uuid4()
    org_id = uuid4()
    fields = [
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="email",
            label="Email",
            target_type=LeadFormFieldTargetType.LEAD_FIELD,
            target_key="email",
            data_type=LeadFormFieldDataType.EMAIL,
            required=True,
        ),
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="start_date",
            label="Start date",
            target_type=LeadFormFieldTargetType.PROPERTY,
            target_key="start_date",
            data_type=LeadFormFieldDataType.DATE,
        ),
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="meeting_time",
            label="Meeting time",
            target_type=LeadFormFieldTargetType.PROPERTY,
            target_key="meeting_time",
            data_type=LeadFormFieldDataType.DATETIME,
        ),
    ]

    _lead_payload, properties = service._map_payload(
        fields,
        {
            "email": "buyer@example.com",
            "start_date": "2026-07-07",
            "meeting_time": "2026-07-07T12:30:00Z",
        },
    )

    assert properties["start_date"] == "2026-07-07"
    assert properties["meeting_time"] == "2026-07-07T12:30:00+00:00"


def test_form_submission_normalizes_company_domain_lead_field() -> None:
    """Mapped forms should store canonical company domains like direct private ingestion."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )
    org_id = uuid4()
    form_id = uuid4()
    fields = [
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="email",
            label="Email",
            target_type=LeadFormFieldTargetType.LEAD_FIELD,
            target_key="email",
            data_type=LeadFormFieldDataType.EMAIL,
            required=True,
        ),
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="domain",
            label="Domain",
            target_type=LeadFormFieldTargetType.LEAD_FIELD,
            target_key="company_domain",
            data_type=LeadFormFieldDataType.TEXT,
        ),
    ]

    lead_payload, _properties = service._map_payload(
        fields,
        {"email": "buyer@example.com", "domain": " https://Example.COM/pricing?utm=ad "},
    )

    assert lead_payload["company_domain"] == "example.com"


def test_form_submission_rejects_oversized_phone_lead_field() -> None:
    """Mapped phone fields should stay within the CRMLead.phone storage bound."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )
    org_id = uuid4()
    form_id = uuid4()
    fields = [
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="phone",
            label="Phone",
            target_type=LeadFormFieldTargetType.LEAD_FIELD,
            target_key="phone",
            data_type=LeadFormFieldDataType.PHONE,
            required=True,
        ),
    ]

    with pytest.raises(LeadFormsServiceError) as exc_info:
        service._map_payload(fields, {"phone": "+" + ("1" * 64)})

    assert exc_info.value.status_code == 422
    assert exc_info.value.data["field"] == "phone"
    assert "Phone value is too long" in exc_info.value.message


def test_form_submission_rejects_oversized_text_lead_field() -> None:
    """Mapped lead fields should respect CRMLead column sizes before DB writes."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )
    org_id = uuid4()
    form_id = uuid4()
    fields = [
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="email",
            label="Email",
            target_type=LeadFormFieldTargetType.LEAD_FIELD,
            target_key="email",
            data_type=LeadFormFieldDataType.EMAIL,
            required=True,
        ),
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="first_name",
            label="First name",
            target_type=LeadFormFieldTargetType.LEAD_FIELD,
            target_key="first_name",
            data_type=LeadFormFieldDataType.TEXT,
            required=True,
        ),
    ]

    with pytest.raises(LeadFormsServiceError) as exc_info:
        service._map_payload(fields, {"email": "buyer@example.com", "first_name": "x" * 256})

    assert exc_info.value.status_code == 422
    assert exc_info.value.data["field"] == "first_name"
    assert "Lead field value is too long" in exc_info.value.message


def test_form_submission_rejects_negative_budget_amount_lead_field() -> None:
    """Mapped budget amounts should respect the CRMLead non-negative invariant."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )
    org_id = uuid4()
    form_id = uuid4()
    fields = [
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="email",
            label="Email",
            target_type=LeadFormFieldTargetType.LEAD_FIELD,
            target_key="email",
            data_type=LeadFormFieldDataType.EMAIL,
            required=True,
        ),
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="budget",
            label="Budget",
            target_type=LeadFormFieldTargetType.LEAD_FIELD,
            target_key="budget_amount",
            data_type=LeadFormFieldDataType.NUMBER,
        ),
    ]

    with pytest.raises(LeadFormsServiceError) as exc_info:
        service._map_payload(fields, {"email": "buyer@example.com", "budget": -1})

    assert exc_info.value.status_code == 422
    assert exc_info.value.data["field"] == "budget"
    assert "Budget amount must be non-negative" in exc_info.value.message


def test_form_submission_rejects_short_budget_currency_lead_field() -> None:
    """Mapped budget currencies should match the direct private lead API contract."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )
    org_id = uuid4()
    form_id = uuid4()
    fields = [
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="email",
            label="Email",
            target_type=LeadFormFieldTargetType.LEAD_FIELD,
            target_key="email",
            data_type=LeadFormFieldDataType.EMAIL,
            required=True,
        ),
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="currency",
            label="Currency",
            target_type=LeadFormFieldTargetType.LEAD_FIELD,
            target_key="budget_currency",
            data_type=LeadFormFieldDataType.TEXT,
        ),
    ]

    with pytest.raises(LeadFormsServiceError) as exc_info:
        service._map_payload(fields, {"email": "buyer@example.com", "currency": "US"})

    assert exc_info.value.status_code == 422
    assert exc_info.value.data["field"] == "currency"
    assert "Budget currency must be a 3-character ISO code" in exc_info.value.message


def test_form_submission_rejects_oversized_default_property_value() -> None:
    """Configured defaults should not bypass mapped-submission payload budgets."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )
    org_id = uuid4()
    form_id = uuid4()
    fields = [
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="email",
            label="Email",
            target_type=LeadFormFieldTargetType.LEAD_FIELD,
            target_key="email",
            data_type=LeadFormFieldDataType.EMAIL,
            required=True,
        ),
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="notes",
            label="Notes",
            target_type=LeadFormFieldTargetType.PROPERTY,
            target_key="notes",
            data_type=LeadFormFieldDataType.TEXT,
            default_value="x" * (MAX_PUBLIC_SCALAR_STRING_LENGTH + 1),
        ),
    ]

    with pytest.raises(LeadFormsServiceError) as exc_info:
        service._map_payload(fields, {"email": "buyer@example.com"})

    assert exc_info.value.status_code == 413
    assert exc_info.value.data["field"] == "notes"


def test_form_submission_rejects_too_many_default_property_nodes() -> None:
    """Many individually-valid defaults should still count toward total payload complexity."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )
    org_id = uuid4()
    form_id = uuid4()
    fields = [
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="email",
            label="Email",
            target_type=LeadFormFieldTargetType.LEAD_FIELD,
            target_key="email",
            data_type=LeadFormFieldDataType.EMAIL,
            required=True,
        )
    ]
    fields.extend(
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key=f"default_{index}",
            label=f"Default {index}",
            target_type=LeadFormFieldTargetType.PROPERTY,
            target_key=f"default_{index}",
            data_type=LeadFormFieldDataType.JSON,
            default_value=list(range(200)),
        )
        for index in range(11)
    )

    with pytest.raises(LeadFormsServiceError) as exc_info:
        service._map_payload(fields, {"email": "buyer@example.com"})

    assert exc_info.value.status_code == 413
    assert exc_info.value.data["code"] == "payload_too_complex"


def test_form_submission_rejects_too_many_default_property_bytes() -> None:
    """Many individually-valid defaults should still count toward total payload bytes."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )
    org_id = uuid4()
    form_id = uuid4()
    fields = [
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="email",
            label="Email",
            target_type=LeadFormFieldTargetType.LEAD_FIELD,
            target_key="email",
            data_type=LeadFormFieldDataType.EMAIL,
            required=True,
        )
    ]
    fields.extend(
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key=f"default_{index}",
            label=f"Default {index}",
            target_type=LeadFormFieldTargetType.PROPERTY,
            target_key=f"default_{index}",
            data_type=LeadFormFieldDataType.TEXT,
            default_value="x" * MAX_PUBLIC_SCALAR_STRING_LENGTH,
        )
        for index in range(27)
    )

    with pytest.raises(LeadFormsServiceError) as exc_info:
        service._map_payload(fields, {"email": "buyer@example.com"})

    assert exc_info.value.status_code == 413
    assert exc_info.value.data["code"] == "payload_too_large"


def test_form_submission_rejects_invalid_lead_custom_select_option() -> None:
    """Submitted property values should honor active lead custom-field options."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )
    form_id = uuid4()
    org_id = uuid4()
    fields = [
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="email",
            label="Email",
            target_type=LeadFormFieldTargetType.LEAD_FIELD,
            target_key="email",
            data_type=LeadFormFieldDataType.EMAIL,
            required=True,
        ),
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="plan",
            label="Plan",
            target_type=LeadFormFieldTargetType.PROPERTY,
            target_key="plan",
            data_type=LeadFormFieldDataType.TEXT,
        ),
    ]
    custom_fields = {
        "plan": SimpleNamespace(
            key="plan",
            data_type=CRMCustomFieldDataType.SELECT,
            is_active=True,
            is_deleted=False,
            options={"values": [{"value": "starter", "active": True}]},
        )
    }

    with pytest.raises(LeadFormsServiceError) as exc_info:
        service._map_payload(
            fields,
            {"email": "buyer@example.com", "plan": "enterprise"},
            lead_custom_fields=custom_fields,
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.data["field"] == "plan"
    assert "Invalid option" in exc_info.value.message


def test_form_submission_accepts_lead_custom_multiselect_values() -> None:
    """JSON form fields can store multiple values into lead multiselect custom fields."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )
    form_id = uuid4()
    org_id = uuid4()
    fields = [
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="email",
            label="Email",
            target_type=LeadFormFieldTargetType.LEAD_FIELD,
            target_key="email",
            data_type=LeadFormFieldDataType.EMAIL,
            required=True,
        ),
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="roles",
            label="Buying Roles",
            target_type=LeadFormFieldTargetType.PROPERTY,
            target_key="buying_roles",
            data_type=LeadFormFieldDataType.JSON,
        ),
    ]
    custom_fields = {
        "buying_roles": SimpleNamespace(
            key="buying_roles",
            data_type=CRMCustomFieldDataType.MULTISELECT,
            is_active=True,
            is_deleted=False,
            options={"values": ["economic_buyer", "technical_buyer"]},
        )
    }

    lead_payload, properties = service._map_payload(
        fields,
        {
            "email": "buyer@example.com",
            "roles": [" economic_buyer ", "technical_buyer"],
        },
        lead_custom_fields=custom_fields,
    )

    assert lead_payload["email"] == "buyer@example.com"
    assert properties == {"buying_roles": ["economic_buyer", "technical_buyer"]}


def test_form_submission_validates_backfilled_normalized_property_mapping() -> None:
    """Legacy target keys should use normalized backfill definitions for validation."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )
    form_id = uuid4()
    org_id = uuid4()
    fields = [
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="email",
            label="Email",
            target_type=LeadFormFieldTargetType.LEAD_FIELD,
            target_key="email",
            data_type=LeadFormFieldDataType.EMAIL,
            required=True,
        ),
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="plan",
            label="Plan",
            target_type=LeadFormFieldTargetType.PROPERTY,
            target_key="Lead.Plan",
            data_type=LeadFormFieldDataType.TEXT,
        ),
    ]
    custom_fields = {
        "lead_plan": SimpleNamespace(
            key="lead_plan",
            data_type=CRMCustomFieldDataType.SELECT,
            is_active=True,
            is_deleted=False,
            options={"values": [{"value": "starter", "active": True}]},
        )
    }

    with pytest.raises(LeadFormsServiceError) as exc_info:
        service._map_payload(
            fields,
            {"email": "buyer@example.com", "plan": "enterprise"},
            lead_custom_fields=custom_fields,
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.data["field"] == "plan"
    assert "Invalid option" in exc_info.value.message


def test_form_submission_rejects_inactive_lead_custom_field() -> None:
    """Existing mappings should not write to inactive lead custom-field definitions."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )
    form_id = uuid4()
    org_id = uuid4()
    fields = [
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="email",
            label="Email",
            target_type=LeadFormFieldTargetType.LEAD_FIELD,
            target_key="email",
            data_type=LeadFormFieldDataType.EMAIL,
            required=True,
        ),
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="plan",
            label="Plan",
            target_type=LeadFormFieldTargetType.PROPERTY,
            target_key="plan",
            data_type=LeadFormFieldDataType.TEXT,
        ),
    ]
    custom_fields = {
        "plan": SimpleNamespace(
            key="plan",
            data_type=CRMCustomFieldDataType.SELECT,
            is_active=False,
            is_deleted=False,
            options={"values": [{"value": "starter", "active": True}]},
        )
    }

    with pytest.raises(LeadFormsServiceError) as exc_info:
        service._map_payload(
            fields,
            {"email": "buyer@example.com", "plan": "starter"},
            lead_custom_fields=custom_fields,
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.data["field"] == "plan"
    assert "not available" in exc_info.value.message


def test_form_submission_preserves_legacy_property_mapping_without_definition() -> None:
    """Legacy forms without a definition row should keep accepting raw property values."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )
    form_id = uuid4()
    org_id = uuid4()
    fields = [
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="email",
            label="Email",
            target_type=LeadFormFieldTargetType.LEAD_FIELD,
            target_key="email",
            data_type=LeadFormFieldDataType.EMAIL,
            required=True,
        ),
        LeadFormField(
            organisation_id=org_id,
            form_id=form_id,
            external_key="legacy_plan",
            label="Legacy plan",
            target_type=LeadFormFieldTargetType.PROPERTY,
            target_key="legacy_plan",
            data_type=LeadFormFieldDataType.TEXT,
        ),
    ]

    _lead_payload, properties = service._map_payload(
        fields,
        {"email": "buyer@example.com", "legacy_plan": "enterprise"},
        lead_custom_fields={},
    )

    assert properties["legacy_plan"] == "enterprise"


@pytest.mark.asyncio
async def test_load_lead_custom_field_definitions_aliases_backfilled_normalized_keys() -> None:
    """Backfilled normalized definitions should still attach to legacy form target keys."""
    definition = SimpleNamespace(key="lead_plan")

    class DefinitionSession:
        async def execute(self, _stmt: object) -> _RowsResult:
            return _RowsResult([definition])

    org_id = uuid4()
    field = LeadFormField(
        organisation_id=org_id,
        form_id=uuid4(),
        external_key="plan",
        label="Plan",
        target_type=LeadFormFieldTargetType.PROPERTY,
        target_key="Lead.Plan",
        data_type=LeadFormFieldDataType.TEXT,
    )
    service = PublicLeadFormSubmissionService(
        org_session=DefinitionSession(),  # type: ignore[arg-type]
        organisation_id=org_id,
        form_id=field.form_id,
        public_form_key="lf_demo",
    )

    definitions = await service._load_lead_custom_field_definitions([field])

    assert definitions["lead_plan"] is definition
    assert definitions["Lead.Plan"] is definition


@pytest.mark.asyncio
async def test_create_form_flushes_org_rows_and_public_key_before_any_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Create flow flushes DB writes before commit, then persists key and commits."""
    log: list[str] = []
    org_session = RecordingAsyncSession("org", log)
    master_session = RecordingAsyncSession("master", log)
    actor_crm_user_id = uuid4()

    async def _generate_public_key(self: LeadFormsService) -> str:
        return "lf_regression_key"

    async def _resolve_actor(self: LeadFormsService):
        return actor_crm_user_id

    monkeypatch.setattr(LeadFormsService, "_generate_unique_public_key", _generate_public_key)
    monkeypatch.setattr(LeadFormsService, "_resolve_actor_crm_user_id", _resolve_actor)

    service = LeadFormsService(
        org_session=org_session,  # type: ignore[arg-type]
        master_session=master_session,  # type: ignore[arg-type]
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    request = CreateLeadFormRequest(
        name="Website demo",
        status=LeadFormStatus.ACTIVE,
        allowed_origins=["https://example.com"],
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
    )

    await service.create_form(request)

    assert log.index("org.commit") > log.index("org.flush")
    assert log.index("org.commit") > log.index("master.flush")
    assert log.index("master.commit") > log.index("org.flush")
    assert log.index("master.commit") > log.index("master.flush")
    assert log.count("org.flush") == 2
    assert log.count("master.flush") == 1


@pytest.mark.asyncio
async def test_property_mapping_requires_known_active_lead_custom_field() -> None:
    """New property mappings should point at lead custom-field definitions."""
    service = LeadFormsService(
        org_session=RecordingAsyncSession("org", []),  # type: ignore[arg-type]
        master_session=RecordingAsyncSession("master", []),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service.org_session.execute = AsyncMock(return_value=_RowsResult([]))  # type: ignore[attr-defined]

    with pytest.raises(LeadFormsServiceError) as exc_info:
        await service._validate_property_field_requests(
            [
                LeadFormFieldRequest(
                    external_key="company_size",
                    label="Company size",
                    target_type=LeadFormFieldTargetType.PROPERTY,
                    target_key="company_size",
                    data_type=LeadFormFieldDataType.NUMBER,
                )
            ]
        )

    assert exc_info.value.status_code == 400
    assert "Unknown lead custom field" in exc_info.value.message


@pytest.mark.asyncio
async def test_property_mapping_accepts_matching_active_lead_custom_field() -> None:
    """Known active lead custom fields can be used as property mappings."""
    service = LeadFormsService(
        org_session=RecordingAsyncSession("org", []),  # type: ignore[arg-type]
        master_session=RecordingAsyncSession("master", []),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service.org_session.execute = AsyncMock(
        return_value=_RowsResult(
            [
                SimpleNamespace(
                    key="company_size",
                    data_type="number",
                    is_active=True,
                    is_deleted=False,
                )
            ]
        )
    )  # type: ignore[attr-defined]

    await service._validate_property_field_requests(
        [
            LeadFormFieldRequest(
                external_key="company_size",
                label="Company size",
                target_type=LeadFormFieldTargetType.PROPERTY,
                target_key="company_size",
                data_type=LeadFormFieldDataType.NUMBER,
            )
        ]
    )


@pytest.mark.asyncio
async def test_property_mapping_accepts_backfilled_normalized_lead_custom_field() -> None:
    """Legacy mappings can resolve the normalized definition generated by backfill."""
    service = LeadFormsService(
        org_session=RecordingAsyncSession("org", []),  # type: ignore[arg-type]
        master_session=RecordingAsyncSession("master", []),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service.org_session.execute = AsyncMock(
        return_value=_RowsResult(
            [
                SimpleNamespace(
                    key="lead_plan",
                    data_type="select",
                    is_active=True,
                    is_deleted=False,
                )
            ]
        )
    )  # type: ignore[attr-defined]

    await service._validate_property_field_requests(
        [
            LeadFormFieldRequest(
                external_key="plan",
                label="Plan",
                target_type=LeadFormFieldTargetType.PROPERTY,
                target_key="Lead.Plan",
                data_type=LeadFormFieldDataType.TEXT,
            )
        ]
    )


@pytest.mark.asyncio
async def test_property_mapping_rejects_incompatible_lead_custom_field_type() -> None:
    """Form field data type must match the target lead custom-field type."""
    service = LeadFormsService(
        org_session=RecordingAsyncSession("org", []),  # type: ignore[arg-type]
        master_session=RecordingAsyncSession("master", []),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service.org_session.execute = AsyncMock(
        return_value=_RowsResult(
            [
                SimpleNamespace(
                    key="company_size",
                    data_type="text",
                    is_active=True,
                    is_deleted=False,
                )
            ]
        )
    )  # type: ignore[attr-defined]

    with pytest.raises(LeadFormsServiceError) as exc_info:
        await service._validate_property_field_requests(
            [
                LeadFormFieldRequest(
                    external_key="company_size",
                    label="Company size",
                    target_type=LeadFormFieldTargetType.PROPERTY,
                    target_key="company_size",
                    data_type=LeadFormFieldDataType.NUMBER,
                )
            ]
        )

    assert exc_info.value.status_code == 400
    assert "does not match" in exc_info.value.message


@pytest.mark.asyncio
async def test_update_form_status_publishes_draft_and_activates_public_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Updating status from draft to active should refresh key state and track both sessions."""
    log: list[str] = []
    org_session = RecordingAsyncSession("org", log)
    master_session = RecordingAsyncSession("master", log)
    actor_crm_user_id = uuid4()
    form = SimpleNamespace(
        form_id=uuid4(),
        status=LeadFormStatus.DRAFT,
        updated_by_user_id=None,
    )
    ingress_key = SimpleNamespace(
        resource_id=form.form_id,
        status="paused",
    )

    async def _get_form(self: LeadFormsService, form_id):
        return form

    async def _load_fields(self: LeadFormsService, form_id):
        return []

    async def _load_keys(self: LeadFormsService, form_ids):
        return {form.form_id: ingress_key}

    async def _resolve_actor(self: LeadFormsService):
        return actor_crm_user_id

    monkeypatch.setattr(LeadFormsService, "_get_form_or_404", _get_form)
    monkeypatch.setattr(LeadFormsService, "_load_fields", _load_fields)
    monkeypatch.setattr(LeadFormsService, "_load_public_form_keys", _load_keys)
    monkeypatch.setattr(LeadFormsService, "_resolve_actor_crm_user_id", _resolve_actor)

    service = LeadFormsService(
        org_session=org_session,  # type: ignore[arg-type]
        master_session=master_session,  # type: ignore[arg-type]
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )

    updated_form, fields, updated_key = await service.update_form_status(
        form.form_id,
        LeadFormStatus.ACTIVE,
    )

    assert updated_form is form
    assert fields == []
    assert updated_key is ingress_key
    assert form.status == LeadFormStatus.ACTIVE
    assert form.updated_by_user_id == actor_crm_user_id
    assert ingress_key.status == "active"
    assert "org.commit" in log
    assert "master.commit" in log


@pytest.mark.asyncio
async def test_update_form_reuses_form_and_fields_and_syncs_public_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Updating a form returns post-commit fields so response serialization stays async-safe."""
    log: list[str] = []
    org_session = RecordingAsyncSession("org", log)
    master_session = RecordingAsyncSession("master", log)
    actor_crm_user_id = uuid4()
    form = SimpleNamespace(
        form_id=uuid4(),
        name="Website demo",
        description="Old",
        status=LeadFormStatus.DRAFT,
        current_version=1,
        settings={},
        updated_by_user_id=None,
    )
    existing_field = SimpleNamespace(
        field_id=uuid4(),
        form_id=form.form_id,
        form_version=1,
        external_key="phone",
        label="Phone",
        target_type=LeadFormFieldTargetType.LEAD_FIELD,
        target_key="phone",
        data_type=LeadFormFieldDataType.PHONE,
        required=False,
        display_order=0,
        default_value=None,
        validation={},
        is_deleted=False,
    )
    removed_field = SimpleNamespace(
        field_id=uuid4(),
        form_id=form.form_id,
        form_version=1,
        external_key="message",
        label="Message",
        target_type=LeadFormFieldTargetType.LEAD_FIELD,
        target_key="message",
        data_type=LeadFormFieldDataType.TEXT,
        required=False,
        display_order=1,
        default_value=None,
        validation={},
        is_deleted=False,
    )
    reloaded_field = SimpleNamespace(
        field_id=existing_field.field_id,
        form_id=form.form_id,
        form_version=2,
        external_key="phone",
        label="Mobile phone",
        target_type=LeadFormFieldTargetType.LEAD_FIELD,
        target_key="phone",
        data_type=LeadFormFieldDataType.PHONE,
        required=True,
        display_order=0,
        default_value=None,
        validation={},
        is_deleted=False,
    )
    ingress_key = SimpleNamespace(
        resource_id=form.form_id,
        status="paused",
        allowed_origins=[],
        key_metadata={},
    )

    async def _get_form(self: LeadFormsService, form_id):
        return form

    load_fields_calls = 0

    async def _load_fields(self: LeadFormsService, form_id):
        nonlocal load_fields_calls
        load_fields_calls += 1
        if load_fields_calls == 1:
            return [existing_field, removed_field]
        return [reloaded_field]

    async def _load_keys(self: LeadFormsService, form_ids):
        return {form.form_id: ingress_key}

    async def _resolve_actor(self: LeadFormsService):
        return actor_crm_user_id

    monkeypatch.setattr(LeadFormsService, "_get_form_or_404", _get_form)
    monkeypatch.setattr(LeadFormsService, "_load_fields", _load_fields)
    monkeypatch.setattr(LeadFormsService, "_load_public_form_keys", _load_keys)
    monkeypatch.setattr(LeadFormsService, "_resolve_actor_crm_user_id", _resolve_actor)

    service = LeadFormsService(
        org_session=org_session,  # type: ignore[arg-type]
        master_session=master_session,  # type: ignore[arg-type]
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    request = UpdateLeadFormRequest(
        name="Website phone form",
        description="Updated",
        status=LeadFormStatus.ACTIVE,
        allowed_origins=["https://example.com"],
        fields=[
            LeadFormFieldRequest(
                external_key="phone",
                label="Mobile phone",
                target_type=LeadFormFieldTargetType.LEAD_FIELD,
                target_key="phone",
                data_type=LeadFormFieldDataType.PHONE,
                required=True,
            )
        ],
    )

    updated_form, updated_fields, updated_key = await service.update_form(
        form.form_id,
        request,
    )

    assert updated_form is form
    assert updated_fields == [reloaded_field]
    assert updated_key is ingress_key
    assert load_fields_calls == 2
    assert form.name == "Website phone form"
    assert form.status == LeadFormStatus.ACTIVE
    assert form.current_version == 2
    assert form.updated_by_user_id == actor_crm_user_id
    assert existing_field.label == "Mobile phone"
    assert existing_field.required is True
    assert existing_field.form_version == 2
    assert removed_field.is_deleted is True
    assert ingress_key.status == "active"
    assert ingress_key.allowed_origins == ["https://example.com"]
    assert ingress_key.key_metadata["form_name"] == "Website phone form"
    assert "org.commit" in log
    assert "master.commit" in log


@pytest.mark.asyncio
async def test_public_form_definition_returns_fields_without_target_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public definitions should omit internal target fields from the submit contract."""
    form = SimpleNamespace(
        form_id=uuid4(),
        name="Website demo",
        description="Book a demo",
        current_version=1,
        settings={"submit_label": "Send"},
    )
    field = SimpleNamespace(
        external_key="email",
        label="Email",
        data_type=LeadFormFieldDataType.EMAIL,
        required=True,
        display_order=0,
        default_value=None,
        validation={"format": "email"},
        target_type=LeadFormFieldTargetType.LEAD_FIELD,
        target_key="email",
    )
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=form.form_id,
        public_form_key="lf_demo",
    )

    async def _load_form(self: PublicLeadFormSubmissionService):
        return form

    async def _load_fields(self: PublicLeadFormSubmissionService, form_id):
        return [field]

    monkeypatch.setattr(PublicLeadFormSubmissionService, "_load_active_form", _load_form)
    monkeypatch.setattr(PublicLeadFormSubmissionService, "_load_active_fields", _load_fields)

    definition = await service.load_definition(origin=None)

    assert definition.public_form_key == "lf_demo"
    assert definition.name == "Website demo"
    assert definition.fields[0].external_key == "email"
    assert not hasattr(definition.fields[0], "target_key")
    assert not hasattr(definition.fields[0], "target_type")


@pytest.mark.asyncio
async def test_public_form_definition_exposes_active_custom_field_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public definitions expose browser-safe active options for custom select fields."""
    form = SimpleNamespace(
        form_id=uuid4(),
        name="Website demo",
        description="Book a demo",
        current_version=1,
        settings={},
    )
    field = SimpleNamespace(
        external_key="plan",
        label="Plan",
        data_type=LeadFormFieldDataType.TEXT,
        required=False,
        display_order=0,
        default_value=None,
        validation={},
        target_type=LeadFormFieldTargetType.PROPERTY,
        target_key="plan",
    )
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=form.form_id,
        public_form_key="lf_demo",
    )
    custom_fields = {
        "plan": SimpleNamespace(
            key="plan",
            data_type=CRMCustomFieldDataType.SELECT,
            is_active=True,
            is_deleted=False,
            options={
                "values": [
                    {"value": "starter", "label": "Starter", "active": True},
                    {"value": "enterprise", "label": "Enterprise", "active": False},
                ]
            },
        )
    }

    async def _load_form(self: PublicLeadFormSubmissionService):
        return form

    async def _load_fields(self: PublicLeadFormSubmissionService, form_id):
        return [field]

    async def _load_custom_fields(self: PublicLeadFormSubmissionService, fields):
        return custom_fields

    monkeypatch.setattr(PublicLeadFormSubmissionService, "_load_active_form", _load_form)
    monkeypatch.setattr(PublicLeadFormSubmissionService, "_load_active_fields", _load_fields)
    monkeypatch.setattr(
        PublicLeadFormSubmissionService,
        "_load_lead_custom_field_definitions",
        _load_custom_fields,
    )

    definition = await service.load_definition(origin=None)

    assert definition.fields[0].options == {
        "values": [{"value": "starter", "label": "Starter", "active": True}]
    }
    assert not hasattr(definition.fields[0], "target_key")
    assert not hasattr(definition.fields[0], "target_type")


@pytest.mark.asyncio
async def test_public_submission_rejects_oversized_string_values() -> None:
    """Reject oversized public form string values with the mapped payload-too-large error."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )

    with pytest.raises(LeadFormsServiceError) as exc_info:
        await service.submit(
            fields_payload={"message": "x" * (MAX_PUBLIC_SCALAR_STRING_LENGTH + 1)},
            context={},
            origin=None,
            user_agent=None,
            submitter_ip=None,
        )

    assert exc_info.value.status_code == 413
    assert exc_info.value.data["code"] == "payload_too_large"
    assert exc_info.value.data["field"] == "message"


@pytest.mark.asyncio
async def test_public_submission_rejects_oversized_context_values() -> None:
    """Context is stored with the submission, so it gets the same payload budget."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )

    with pytest.raises(LeadFormsServiceError) as exc_info:
        await service.submit(
            fields_payload={"email": "buyer@example.com"},
            context={"tracking": "x" * (MAX_PUBLIC_SCALAR_STRING_LENGTH + 1)},
            origin=None,
            user_agent=None,
            submitter_ip=None,
        )

    assert exc_info.value.status_code == 413
    assert exc_info.value.data["code"] == "payload_too_large"
    assert exc_info.value.data["field"] == "tracking"


def test_form_submission_rejects_unknown_field_keys() -> None:
    """Submitted fields must be declared on the form instead of becoming ad hoc properties."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )
    form_id = uuid4()
    fields = [
        LeadFormField(
            organisation_id=uuid4(),
            form_id=form_id,
            external_key="email",
            label="Email",
            target_type=LeadFormFieldTargetType.LEAD_FIELD,
            target_key="email",
            data_type=LeadFormFieldDataType.EMAIL,
            required=True,
        )
    ]

    with pytest.raises(LeadFormsServiceError) as exc_info:
        service._map_payload(fields, {"email": "buyer@example.com", "company": "Acme"})

    assert exc_info.value.status_code == 422
    assert exc_info.value.data["code"] == "unknown_field"
    assert exc_info.value.data["field"] == "company"


def test_public_spam_controls_drop_empty_honeypot_field() -> None:
    """Configured honeypot fields should not trip normal unknown-field validation when empty."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )

    filtered = service._apply_public_spam_controls(
        {"spam_protection": {"honeypot_field": "website"}},
        {"email": "buyer@example.com", "website": "  "},
        {},
    )

    assert filtered == {"email": "buyer@example.com"}


def test_public_spam_controls_reject_filled_honeypot_field() -> None:
    """Bots that fill a hidden honeypot field should be rejected before lead creation."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )

    with pytest.raises(LeadFormsServiceError) as exc_info:
        service._apply_public_spam_controls(
            {"spam_protection": {"honeypot_field": "website"}},
            {"email": "buyer@example.com", "website": "https://spam.example"},
            {},
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.data["code"] == "spam_rejected"
    assert exc_info.value.data["field"] == "website"


def test_public_spam_controls_reject_too_fast_submission() -> None:
    """A minimum fill-time hook catches obvious scripted public submissions."""
    service = PublicLeadFormSubmissionService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="lf_demo",
    )
    rendered_at = datetime.now(UTC) - timedelta(seconds=1)

    with pytest.raises(LeadFormsServiceError) as exc_info:
        service._apply_public_spam_controls(
            {"spam_protection": {"min_fill_seconds": 3}},
            {"email": "buyer@example.com"},
            {"rendered_at": rendered_at.isoformat()},
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.data["code"] == "spam_rejected"
    assert exc_info.value.data["field"] == "rendered_at"


@pytest.mark.asyncio
async def test_public_form_submission_replays_by_persisted_idempotency_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public retries with the same idempotency key return the existing submission/lead pair."""
    log: list[str] = []
    org_session = RecordingAsyncSession("org", log)
    org_id = uuid4()
    lead = CRMLead(organisation_id=org_id, email="buyer@example.com")
    submission = LeadFormSubmission(
        organisation_id=org_id,
        form_id=uuid4(),
        form_version=1,
        lead_id=lead.lead_id,
        idempotency_key="public-form-123",
    )
    service = PublicLeadFormSubmissionService(
        org_session=org_session,  # type: ignore[arg-type]
        organisation_id=org_id,
        form_id=submission.form_id,
        public_form_key="lf_demo",
    )
    calls: list[str] = []

    async def _find_existing(
        self: PublicLeadFormSubmissionService,
        *,
        idempotency_key: str,
    ):
        calls.append(idempotency_key)
        return lead, submission

    monkeypatch.setattr(
        PublicLeadFormSubmissionService,
        "_find_existing_public_submission",
        _find_existing,
    )

    returned_lead, returned_submission = await service.submit(
        fields_payload={"email": "buyer@example.com"},
        context={},
        origin=None,
        user_agent=None,
        submitter_ip=None,
        idempotency_key=" public-form-123 ",
    )

    assert returned_lead is lead
    assert returned_submission is submission
    assert calls == ["public-form-123"]
    assert org_session.added == []


@pytest.mark.asyncio
async def test_public_form_submission_unique_race_reloads_existing_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A public idempotency unique conflict should rollback and return the winner row."""
    log: list[str] = []
    existing_lead = CRMLead(organisation_id=uuid4(), email="buyer@example.com")
    existing_submission = LeadFormSubmission(
        organisation_id=existing_lead.organisation_id,
        form_id=uuid4(),
        form_version=1,
        lead_id=existing_lead.lead_id,
        idempotency_key="public-form-123",
    )
    form = LeadForm(
        organisation_id=existing_lead.organisation_id,
        form_id=existing_submission.form_id,
        name="Website demo",
        status=LeadFormStatus.ACTIVE,
        current_version=1,
        settings={},
    )
    field = LeadFormField(
        organisation_id=existing_lead.organisation_id,
        form_id=existing_submission.form_id,
        external_key="email",
        label="Email",
        target_type=LeadFormFieldTargetType.LEAD_FIELD,
        target_key="email",
        data_type=LeadFormFieldDataType.EMAIL,
        required=True,
    )
    org_session = RecordingAsyncSession(
        "org",
        log,
        flush_error=IntegrityError("insert", {}, Exception("unique violation")),
    )
    service = PublicLeadFormSubmissionService(
        org_session=org_session,  # type: ignore[arg-type]
        organisation_id=existing_lead.organisation_id,
        form_id=existing_submission.form_id,
        public_form_key="lf_demo",
    )
    lookup_count = 0

    async def _find_existing(
        self: PublicLeadFormSubmissionService,
        *,
        idempotency_key: str,
    ):
        nonlocal lookup_count
        lookup_count += 1
        return None if lookup_count == 1 else (existing_lead, existing_submission)

    async def _load_form(self: PublicLeadFormSubmissionService):
        return form

    async def _load_fields(self: PublicLeadFormSubmissionService, form_id):
        return [field]

    monkeypatch.setattr(
        PublicLeadFormSubmissionService,
        "_find_existing_public_submission",
        _find_existing,
    )
    monkeypatch.setattr(PublicLeadFormSubmissionService, "_load_active_form", _load_form)
    monkeypatch.setattr(PublicLeadFormSubmissionService, "_load_active_fields", _load_fields)

    returned_lead, returned_submission = await service.submit(
        fields_payload={"email": "buyer@example.com"},
        context={},
        origin=None,
        user_agent=None,
        submitter_ip=None,
        idempotency_key="public-form-123",
    )

    assert returned_lead is existing_lead
    assert returned_submission is existing_submission
    assert "org.rollback" in log
    assert lookup_count == 2


@pytest.mark.asyncio
async def test_private_form_submission_persists_idempotency_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Form private-key submissions store replay keys on the submission row."""
    log: list[str] = []
    org_session = RecordingAsyncSession("org", log)
    org_id = uuid4()
    form = LeadForm(
        organisation_id=org_id,
        form_id=uuid4(),
        name="Website demo",
        status=LeadFormStatus.ACTIVE,
        current_version=3,
        settings={},
    )
    field = LeadFormField(
        organisation_id=org_id,
        form_id=form.form_id,
        external_key="email",
        label="Email",
        target_type=LeadFormFieldTargetType.LEAD_FIELD,
        target_key="email",
        data_type=LeadFormFieldDataType.EMAIL,
        required=True,
    )
    service = PublicLeadFormSubmissionService(
        org_session=org_session,  # type: ignore[arg-type]
        organisation_id=org_id,
        form_id=form.form_id,
        public_form_key="skid_form",
    )

    async def _load_form(self: PublicLeadFormSubmissionService):
        return form

    async def _load_fields(self: PublicLeadFormSubmissionService, form_id):
        return [field]

    async def _find_existing(
        self: PublicLeadFormSubmissionService,
        *,
        key_id: str,
        idempotency_key: str,
    ):
        return None

    monkeypatch.setattr(PublicLeadFormSubmissionService, "_load_active_form", _load_form)
    monkeypatch.setattr(PublicLeadFormSubmissionService, "_load_active_fields", _load_fields)
    monkeypatch.setattr(
        PublicLeadFormSubmissionService,
        "_find_existing_private_submission",
        _find_existing,
    )

    lead, submission, replayed = await service.submit_private(
        fields_payload={"email": "buyer@example.com"},
        context={},
        key_id="skid_form",
        idempotency_key=" form-lead-123 ",
        user_agent="pytest",
        submitter_ip="203.0.113.10",
    )

    assert replayed is False
    assert isinstance(lead, CRMLead)
    assert isinstance(submission, LeadFormSubmission)
    assert submission in org_session.added
    assert submission.ingestion_key_id == "skid_form"
    assert submission.idempotency_key == "form-lead-123"
    assert submission.context["_copilotgtm"]["ingestion"]["idempotency_key"] == "form-lead-123"


@pytest.mark.asyncio
async def test_private_form_submission_rejects_oversized_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mapped submissions should bound User-Agent before String(1000) persistence."""
    org_session = RecordingAsyncSession("org", [])
    org_id = uuid4()
    form = LeadForm(
        organisation_id=org_id,
        form_id=uuid4(),
        name="Website demo",
        status=LeadFormStatus.ACTIVE,
        current_version=3,
        settings={},
    )
    field = LeadFormField(
        organisation_id=org_id,
        form_id=form.form_id,
        external_key="email",
        label="Email",
        target_type=LeadFormFieldTargetType.LEAD_FIELD,
        target_key="email",
        data_type=LeadFormFieldDataType.EMAIL,
        required=True,
    )
    service = PublicLeadFormSubmissionService(
        org_session=org_session,  # type: ignore[arg-type]
        organisation_id=org_id,
        form_id=form.form_id,
        public_form_key="skid_form",
    )

    async def _load_form(self: PublicLeadFormSubmissionService):
        return form

    async def _load_fields(self: PublicLeadFormSubmissionService, form_id):
        return [field]

    async def _find_existing(
        self: PublicLeadFormSubmissionService,
        *,
        key_id: str,
        idempotency_key: str,
    ):
        return None

    monkeypatch.setattr(PublicLeadFormSubmissionService, "_load_active_form", _load_form)
    monkeypatch.setattr(PublicLeadFormSubmissionService, "_load_active_fields", _load_fields)
    monkeypatch.setattr(
        PublicLeadFormSubmissionService,
        "_find_existing_private_submission",
        _find_existing,
    )

    with pytest.raises(LeadFormsServiceError) as exc_info:
        await service.submit_private(
            fields_payload={"email": "buyer@example.com"},
            context={},
            key_id="skid_form",
            idempotency_key="form-lead-123",
            user_agent="x" * 1001,
            submitter_ip="203.0.113.10",
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.data["code"] == "invalid_user_agent"
    assert org_session.added == []


@pytest.mark.asyncio
async def test_private_form_submission_rejects_oversized_submitter_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mapped submissions should bound submitter IP before String(64) persistence."""
    org_session = RecordingAsyncSession("org", [])
    org_id = uuid4()
    form = LeadForm(
        organisation_id=org_id,
        form_id=uuid4(),
        name="Website demo",
        status=LeadFormStatus.ACTIVE,
        current_version=3,
        settings={},
    )
    field = LeadFormField(
        organisation_id=org_id,
        form_id=form.form_id,
        external_key="email",
        label="Email",
        target_type=LeadFormFieldTargetType.LEAD_FIELD,
        target_key="email",
        data_type=LeadFormFieldDataType.EMAIL,
        required=True,
    )
    service = PublicLeadFormSubmissionService(
        org_session=org_session,  # type: ignore[arg-type]
        organisation_id=org_id,
        form_id=form.form_id,
        public_form_key="skid_form",
    )

    async def _load_form(self: PublicLeadFormSubmissionService):
        return form

    async def _load_fields(self: PublicLeadFormSubmissionService, form_id):
        return [field]

    async def _find_existing(
        self: PublicLeadFormSubmissionService,
        *,
        key_id: str,
        idempotency_key: str,
    ):
        return None

    monkeypatch.setattr(PublicLeadFormSubmissionService, "_load_active_form", _load_form)
    monkeypatch.setattr(PublicLeadFormSubmissionService, "_load_active_fields", _load_fields)
    monkeypatch.setattr(
        PublicLeadFormSubmissionService,
        "_find_existing_private_submission",
        _find_existing,
    )

    with pytest.raises(LeadFormsServiceError) as exc_info:
        await service.submit_private(
            fields_payload={"email": "buyer@example.com"},
            context={},
            key_id="skid_form",
            idempotency_key="form-lead-123",
            user_agent="pytest",
            submitter_ip="x" * 65,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.data["code"] == "invalid_submitter_ip"
    assert org_session.added == []


@pytest.mark.asyncio
async def test_private_form_submission_rejects_oversized_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Header replay tokens should be bounded before they hit String(255) storage."""
    service = PublicLeadFormSubmissionService(
        org_session=RecordingAsyncSession("org", []),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="skid_form",
    )
    replay_lookup_called = False

    async def _find_existing(
        self: PublicLeadFormSubmissionService,
        *,
        key_id: str,
        idempotency_key: str,
    ):
        nonlocal replay_lookup_called
        replay_lookup_called = True
        return None

    monkeypatch.setattr(
        PublicLeadFormSubmissionService,
        "_find_existing_private_submission",
        _find_existing,
    )

    with pytest.raises(LeadFormsServiceError) as exc_info:
        await service.submit_private(
            fields_payload={"email": "buyer@example.com"},
            context={},
            key_id="skid_form",
            idempotency_key="x" * 256,
            user_agent=None,
            submitter_ip=None,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.data["code"] == "invalid_idempotency_key"
    assert replay_lookup_called is False


@pytest.mark.asyncio
async def test_private_form_submission_rejects_oversized_key_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Form private key ids should be bounded before they hit String(128) storage."""
    service = PublicLeadFormSubmissionService(
        org_session=RecordingAsyncSession("org", []),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        form_id=uuid4(),
        public_form_key="skid_form",
    )
    replay_lookup_called = False

    async def _find_existing(
        self: PublicLeadFormSubmissionService,
        *,
        key_id: str,
        idempotency_key: str,
    ):
        nonlocal replay_lookup_called
        replay_lookup_called = True
        return None

    monkeypatch.setattr(
        PublicLeadFormSubmissionService,
        "_find_existing_private_submission",
        _find_existing,
    )

    with pytest.raises(LeadFormsServiceError) as exc_info:
        await service.submit_private(
            fields_payload={"email": "buyer@example.com"},
            context={},
            key_id="x" * 129,
            idempotency_key="form-lead-123",
            user_agent=None,
            submitter_ip=None,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.data["code"] == "invalid_key_id"
    assert replay_lookup_called is False


@pytest.mark.asyncio
async def test_private_form_submission_replays_by_persisted_idempotency_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry lookup returns the existing submission/lead pair before any write."""
    log: list[str] = []
    org_session = RecordingAsyncSession("org", log)
    org_id = uuid4()
    lead = CRMLead(organisation_id=org_id, email="buyer@example.com")
    submission = LeadFormSubmission(
        organisation_id=org_id,
        form_id=uuid4(),
        form_version=1,
        lead_id=lead.lead_id,
    )
    service = PublicLeadFormSubmissionService(
        org_session=org_session,  # type: ignore[arg-type]
        organisation_id=org_id,
        form_id=submission.form_id,
        public_form_key="skid_form",
    )
    calls: list[tuple[str, str]] = []

    async def _find_existing(
        self: PublicLeadFormSubmissionService,
        *,
        key_id: str,
        idempotency_key: str,
    ):
        calls.append((key_id, idempotency_key))
        return lead, submission

    monkeypatch.setattr(
        PublicLeadFormSubmissionService,
        "_find_existing_private_submission",
        _find_existing,
    )

    returned_lead, returned_submission, replayed = await service.submit_private(
        fields_payload={"email": "buyer@example.com"},
        context={},
        key_id="skid_form",
        idempotency_key="form-lead-123",
        user_agent=None,
        submitter_ip=None,
    )

    assert returned_lead is lead
    assert returned_submission is submission
    assert replayed is True
    assert calls == [("skid_form", "form-lead-123")]
    assert org_session.added == []


@pytest.mark.asyncio
async def test_private_form_submission_unique_race_reloads_existing_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB unique conflict should rollback and replay the committed submission pair."""
    log: list[str] = []
    existing_lead = CRMLead(organisation_id=uuid4(), email="buyer@example.com")
    existing_submission = LeadFormSubmission(
        organisation_id=existing_lead.organisation_id,
        form_id=uuid4(),
        form_version=1,
        lead_id=existing_lead.lead_id,
    )
    form = LeadForm(
        organisation_id=existing_lead.organisation_id,
        form_id=existing_submission.form_id,
        name="Website demo",
        status=LeadFormStatus.ACTIVE,
        current_version=1,
        settings={},
    )
    field = LeadFormField(
        organisation_id=existing_lead.organisation_id,
        form_id=existing_submission.form_id,
        external_key="email",
        label="Email",
        target_type=LeadFormFieldTargetType.LEAD_FIELD,
        target_key="email",
        data_type=LeadFormFieldDataType.EMAIL,
        required=True,
    )
    org_session = RecordingAsyncSession(
        "org",
        log,
        flush_error=IntegrityError("insert", {}, Exception("unique violation")),
    )
    service = PublicLeadFormSubmissionService(
        org_session=org_session,  # type: ignore[arg-type]
        organisation_id=existing_lead.organisation_id,
        form_id=existing_submission.form_id,
        public_form_key="skid_form",
    )
    lookup_count = 0

    async def _find_existing(
        self: PublicLeadFormSubmissionService,
        *,
        key_id: str,
        idempotency_key: str,
    ):
        nonlocal lookup_count
        lookup_count += 1
        return None if lookup_count == 1 else (existing_lead, existing_submission)

    monkeypatch.setattr(
        PublicLeadFormSubmissionService,
        "_find_existing_private_submission",
        _find_existing,
    )

    async def _load_form(self: PublicLeadFormSubmissionService):
        return form

    async def _load_fields(self: PublicLeadFormSubmissionService, form_id):
        return [field]

    monkeypatch.setattr(PublicLeadFormSubmissionService, "_load_active_form", _load_form)
    monkeypatch.setattr(PublicLeadFormSubmissionService, "_load_active_fields", _load_fields)

    returned_lead, returned_submission, replayed = await service.submit_private(
        fields_payload={"email": "buyer@example.com"},
        context={},
        key_id="skid_form",
        idempotency_key="form-lead-123",
        user_agent=None,
        submitter_ip=None,
    )

    assert returned_lead is existing_lead
    assert returned_submission is existing_submission
    assert replayed is True
    assert "org.rollback" in log
    assert lookup_count == 2
