"""Regression tests for lead-form service transaction ordering and payload guards.

The suite uses deterministic in-memory helpers to assert orchestration rules such as
public-key creation ordering, update-state transitions, and submission validation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
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
from src.domain.models.organisation_tables.sync.crm.lead_forms import (
    LeadForm,
    LeadFormField,
    LeadFormFieldDataType,
    LeadFormFieldTargetType,
    LeadFormStatus,
    LeadFormSubmission,
)
from src.domain.models.organisation_tables.sync.crm.leads import CRMLead
from src.service.crm.access.lead_forms_service import (
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
    """Updating a form reuses canonical fields while activating public metadata updates."""
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
    ingress_key = SimpleNamespace(
        resource_id=form.form_id,
        status="paused",
        allowed_origins=[],
        key_metadata={},
    )

    async def _get_form(self: LeadFormsService, form_id):
        return form

    async def _load_fields(self: LeadFormsService, form_id):
        return [existing_field, removed_field]

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
    assert updated_fields == [existing_field]
    assert updated_key is ingress_key
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
