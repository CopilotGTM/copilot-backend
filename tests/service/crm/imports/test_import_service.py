from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.crm.imports import (
    CRMImportRow,
    CRMImportRowStatus,
)
from src.service.crm.accounts.contracts import AccountWriteResult
from src.service.crm.contacts.contracts import ContactWriteResult, ContactWriteServiceError
from src.service.crm.imports.import_service import (
    CRMImportRowsResult,
    CRMImportService,
    CRMImportServiceError,
    _deal_payload_from_values,
    _increment_counts,
    _parse_import_custom_value,
    _parse_import_number,
)


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _ScalarRows(self._rows)


class _Session:
    def __init__(self, *execute_rows):
        self.execute_rows = list(execute_rows)
        self.added = []
        self.flush_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.on_commit = None

    async def execute(self, _stmt):
        return _Result(self.execute_rows.pop(0) if self.execute_rows else [])

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        self.flush_count += 1

    async def commit(self):
        self.commit_count += 1
        if self.on_commit:
            self.on_commit()

    async def rollback(self):
        self.rollback_count += 1


class _Storage:
    def __init__(self, *, blob_content=b"Email,Company\njane@example.com,Acme\n", size=128):
        self.blob_content = blob_content
        self.size = size
        self.write_sas_paths = []

    def plan_crm_import_path(self, *, user_id, import_id, original_filename):
        return f"crm_imports/{user_id}/{import_id}/{original_filename}"

    async def generate_write_sas(self, blob_path):
        self.write_sas_paths.append(blob_path)
        return f"https://upload.example/{blob_path}"

    async def get_blob_properties(self, _blob_path):
        return {"size": self.size, "content_type": "text/csv"}

    async def get_blob_content(self, _blob_path):
        return self.blob_content


def _job(*, org_id, user_id, metadata=None, status=JobStatus.PENDING):
    return DealJobs(
        organisation_id=org_id,
        user_id=user_id,
        job_type=DealJobType.CRM_CSV_IMPORT,
        deal_id=None,
        status=status,
        job_metadata=metadata or {},
    )


def test_invalid_standard_amount_is_a_row_validation_error() -> None:
    with pytest.raises(CRMImportServiceError, match="Invalid Deal amount"):
        _deal_payload_from_values(
            {"amount": "not-money"},
            column_formats={},
            mapping={"Amount": "deal.amount"},
        )


def test_batch_counts_are_added_to_committed_progress_once() -> None:
    org_id = uuid4()
    job_id = uuid4()
    row = CRMImportRow(
        import_job_id=job_id,
        organisation_id=org_id,
        row_number=2,
        raw_values={},
        normalized_values={"_audit": {"account_outcome": "matched", "contact_outcome": "created"}},
        status=CRMImportRowStatus.CREATED,
    )

    counts = _increment_counts(
        {
            "total_rows": 3,
            "processed_rows": 1,
            "successful_rows": 1,
            "created_deals": 1,
            "created_accounts": 1,
            "matched_accounts": 0,
            "created_contacts": 0,
            "matched_contacts": 1,
            "failed_rows": 0,
            "skipped_rows": 0,
        },
        [row],
        total_rows=3,
    )

    assert counts == {
        "total_rows": 3,
        "processed_rows": 2,
        "successful_rows": 2,
        "created_deals": 2,
        "created_accounts": 1,
        "matched_accounts": 1,
        "created_contacts": 1,
        "matched_contacts": 1,
        "failed_rows": 0,
        "skipped_rows": 0,
    }


@pytest.mark.asyncio
async def test_presign_creates_deal_job_header_and_write_sas():
    """Presign persists pipeline-aware configuration before issuing upload access."""
    org_id = uuid4()
    user_id = uuid4()
    storage = _Storage()
    session = _Session()
    service = CRMImportService(session, org_id, user_id, storage=storage)
    service._validate_configuration = AsyncMock()
    pipeline_id = uuid4()
    stage_id = uuid4()

    result = await service.presign("deals.csv", pipeline_id=pipeline_id, default_stage_id=stage_id)

    job = session.added[0]
    assert isinstance(job, DealJobs)
    assert job.job_type == DealJobType.CRM_CSV_IMPORT
    assert job.deal_id is None
    assert job.status == JobStatus.PENDING
    assert job.job_metadata["filename"] == "deals.csv"
    assert job.job_metadata["pipeline_id"] == str(pipeline_id)
    assert job.job_metadata["default_stage_id"] == str(stage_id)
    assert job.job_metadata["storage_path"].startswith(f"crm_imports/{user_id}/{job.job_id}/")
    assert result.import_id == job.job_id
    assert result.upload_url.startswith("https://upload.example/")
    assert session.commit_count == 1


@pytest.mark.asyncio
async def test_finalize_scopes_job_and_moves_to_analyzing():
    org_id = uuid4()
    user_id = uuid4()
    job = _job(
        org_id=org_id,
        user_id=user_id,
        metadata={"storage_path": "crm_imports/file.csv", "crm_import_status": "uploading"},
    )
    storage = _Storage(size=256)
    session = _Session([job])
    service = CRMImportService(session, org_id, user_id, storage=storage)

    result = await service.finalize(job.job_id)

    assert result.job.job_metadata["crm_import_status"] == "analyzing"
    assert result.job.job_metadata["file_size_bytes"] == 256
    assert session.commit_count == 1


@pytest.mark.asyncio
async def test_finalize_retry_does_not_regress_analyzed_state():
    org_id = uuid4()
    user_id = uuid4()
    job = _job(
        org_id=org_id,
        user_id=user_id,
        metadata={"crm_import_status": "ready_for_mapping"},
    )
    session = _Session([job])
    service = CRMImportService(session, org_id, user_id, storage=_Storage())

    result = await service.finalize(job.job_id)

    assert result.job.job_metadata["crm_import_status"] == "ready_for_mapping"
    assert session.commit_count == 0


@pytest.mark.asyncio
async def test_import_job_lookup_filters_by_org_and_user():
    org_id = uuid4()
    user_id = uuid4()
    session = _Session([])
    service = CRMImportService(session, org_id, user_id, storage=_Storage())

    with pytest.raises(CRMImportServiceError) as exc_info:
        await service.finalize(uuid4())

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_analyze_downloads_parses_maps_and_does_not_duplicate_existing_rows():
    """Analysis stores representative rows and remains idempotent on task redelivery."""
    org_id = uuid4()
    user_id = uuid4()
    job = _job(
        org_id=org_id,
        user_id=user_id,
        metadata={
            "storage_path": "crm_imports/file.csv",
            "crm_import_status": "analyzing",
            "pipeline_id": str(uuid4()),
        },
    )
    session = _Session([job], [])
    service = CRMImportService(session, org_id, user_id, storage=_Storage())
    service.get_targets = AsyncMock(return_value=SimpleNamespace(targets=[], stages=[]))

    result = await service.analyze(job.job_id)

    rows = [row for row in session.added if isinstance(row, CRMImportRow)]
    assert len(rows) == 1
    assert rows[0].row_number == 1
    assert rows[0].status == CRMImportRowStatus.PREVIEW
    assert result.job.job_metadata["crm_import_status"] == "ready_for_mapping"
    assert result.job.job_metadata["analysis"]["total_rows"] == 1
    assert result.job.job_metadata["mapping_suggestions"][0]["header"] == "Email"

    retry_session = _Session([job], rows)
    retry_service = CRMImportService(retry_session, org_id, user_id, storage=_Storage())
    retry_service.get_targets = AsyncMock(return_value=SimpleNamespace(targets=[], stages=[]))
    await retry_service.analyze(job.job_id)

    assert not any(isinstance(row, CRMImportRow) for row in retry_session.added)


@pytest.mark.asyncio
async def test_save_mapping_validates_supported_fields_and_start_retries_dispatch():
    """A saved complete draft starts once and can republish after queue failure."""
    org_id = uuid4()
    user_id = uuid4()
    pipeline_id = uuid4()
    stage_id = uuid4()
    job = _job(
        org_id=org_id,
        user_id=user_id,
        metadata={
            "crm_import_status": "ready_for_mapping",
            "analysis": {"headers": ["Deal Name", "Company", "Email"], "total_rows": 1},
            "pipeline_id": str(pipeline_id),
            "default_stage_id": str(stage_id),
        },
    )
    row = CRMImportRow(
        import_job_id=job.job_id,
        organisation_id=org_id,
        row_number=1,
        raw_values={"Deal Name": "Expansion", "Company": "Acme", "Email": "buyer@example.com"},
        status=CRMImportRowStatus.PREVIEW,
    )
    sent = []

    async def send_task(*, organisation_id, import_job_id):
        assert session.commit_count == 2
        sent.append((organisation_id, import_job_id))

    session = _Session([job], [row], [job], [row], [job])
    service = CRMImportService(session, org_id, user_id, storage=_Storage(), task_sender=send_task)
    service._validate_configuration = AsyncMock()
    service._validate_mapping_draft = AsyncMock()

    await service.save_mapping(
        job.job_id,
        {
            "Deal Name": "deal.name",
            "Company": "account.name",
            "Email": "contact.email",
        },
        pipeline_id=pipeline_id,
        default_stage_id=stage_id,
        options={"update_existing": False},
    )
    started = await service.start(job.job_id)
    duplicate = await service.start(job.job_id)

    assert started.job.status == JobStatus.IN_PROGRESS
    assert duplicate.job.status == JobStatus.IN_PROGRESS
    assert sent == [(org_id, job.job_id), (org_id, job.job_id)]
    assert job.job_metadata["mapping"] == {
        "Deal Name": "deal.name",
        "Company": "account.name",
        "Email": "contact.email",
    }


@pytest.mark.asyncio
async def test_save_mapping_requires_completed_analysis_and_preview_rows():
    org_id = uuid4()
    user_id = uuid4()
    uploading_job = _job(
        org_id=org_id,
        user_id=user_id,
        metadata={"crm_import_status": "uploading"},
    )
    ready_without_rows = _job(
        org_id=org_id,
        user_id=user_id,
        metadata={
            "crm_import_status": "ready_for_mapping",
            "analysis": {"headers": ["Email"], "total_rows": 1},
        },
    )
    service = CRMImportService(
        _Session([uploading_job], [ready_without_rows], []),
        org_id,
        user_id,
        storage=_Storage(),
    )

    with pytest.raises(CRMImportServiceError) as wrong_state:
        await service.save_mapping(uploading_job.job_id, {"Email": "contact.email"})

    with pytest.raises(CRMImportServiceError) as no_rows:
        await service.save_mapping(ready_without_rows.job_id, {"Email": "contact.email"})

    assert wrong_state.value.status_code == 409
    assert no_rows.value.status_code == 409


@pytest.mark.asyncio
async def test_save_mapping_rejects_duplicate_target_fields():
    org_id = uuid4()
    user_id = uuid4()
    job = _job(
        org_id=org_id,
        user_id=user_id,
        metadata={
            "crm_import_status": "ready_for_mapping",
            "analysis": {"headers": ["Email", "Work Email"], "total_rows": 1},
        },
    )
    row = CRMImportRow(
        import_job_id=job.job_id,
        organisation_id=org_id,
        row_number=1,
        raw_values={"Email": "primary@example.com", "Work Email": "work@example.com"},
        status=CRMImportRowStatus.PREVIEW,
    )
    session = _Session([job], [row])
    service = CRMImportService(session, org_id, user_id, storage=_Storage())

    with pytest.raises(CRMImportServiceError) as exc_info:
        await service.save_mapping(
            job.job_id,
            {"Email": "contact.email", "Work Email": "contact.email"},
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.data == {"duplicate_target_fields": ["contact.email"]}
    assert session.commit_count == 0


@pytest.mark.asyncio
async def test_mapping_requires_explicit_order_for_ambiguous_numeric_dates():
    service = CRMImportService(_Session(), uuid4(), uuid4(), storage=_Storage())
    targets = [
        {
            "target_field": "deal.name",
            "value_type": "text",
            "required": True,
            "custom_field_id": None,
        },
        {
            "target_field": "account.name",
            "value_type": "text",
            "required": False,
            "custom_field_id": None,
        },
        {
            "target_field": "contact.email",
            "value_type": "email",
            "required": False,
            "custom_field_id": None,
        },
        {
            "target_field": "deal.expected_close_date",
            "value_type": "date",
            "required": False,
            "custom_field_id": None,
        },
    ]
    service.get_targets = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(targets=targets)
    )
    mapping = {
        "Deal": "deal.name",
        "Account": "account.name",
        "Email": "contact.email",
        "Close": "deal.expected_close_date",
    }
    metadata = {
        "analysis": {
            "headers": list(mapping),
            "sample_rows": [{"Close": "06/07/2026"}],
        }
    }

    with pytest.raises(CRMImportServiceError) as exc_info:
        await service._validate_mapping_draft(
            metadata=metadata,
            mapping=mapping,
            ignored_columns=[],
            column_formats={},
            pipeline_id=uuid4(),
        )

    assert exc_info.value.data == {"columns": ["Close"]}
    await service._validate_mapping_draft(
        metadata=metadata,
        mapping=mapping,
        ignored_columns=[],
        column_formats={"Close": "DMY"},
        pipeline_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_start_requires_analysis_rows_and_saved_mapping():
    org_id = uuid4()
    user_id = uuid4()
    no_analysis = _job(
        org_id=org_id,
        user_id=user_id,
        metadata={"crm_import_status": "ready_for_mapping", "mapping": {"Email": "contact.email"}},
    )
    no_rows = _job(
        org_id=org_id,
        user_id=user_id,
        metadata={
            "crm_import_status": "ready_for_mapping",
            "analysis": {"headers": ["Email"], "total_rows": 1},
            "mapping": {"Email": "contact.email"},
        },
    )
    no_mapping = _job(
        org_id=org_id,
        user_id=user_id,
        metadata={
            "crm_import_status": "ready_for_mapping",
            "analysis": {"headers": ["Email"], "total_rows": 1},
        },
    )
    service = CRMImportService(
        _Session([no_analysis], [no_rows], [], [no_mapping], []),
        org_id,
        user_id,
        storage=_Storage(),
    )

    with pytest.raises(CRMImportServiceError) as missing_analysis:
        await service.start(no_analysis.job_id)
    with pytest.raises(CRMImportServiceError) as missing_rows:
        await service.start(no_rows.job_id)
    with pytest.raises(CRMImportServiceError) as missing_mapping:
        await service.start(no_mapping.job_id)

    assert missing_analysis.value.status_code == 409
    assert missing_rows.value.status_code == 409
    assert missing_mapping.value.status_code == 409


@pytest.mark.asyncio
async def test_start_retries_task_send_when_import_is_already_importing():
    """A queue publication failure leaves importing state safe for start redelivery."""
    org_id = uuid4()
    user_id = uuid4()
    pipeline_id = uuid4()
    stage_id = uuid4()
    job = _job(
        org_id=org_id,
        user_id=user_id,
        metadata={
            "crm_import_status": "ready_for_mapping",
            "analysis": {"headers": ["Email"], "total_rows": 1},
            "mapping": {"Email": "contact.email"},
            "pipeline_id": str(pipeline_id),
            "default_stage_id": str(stage_id),
        },
    )
    row = CRMImportRow(
        import_job_id=job.job_id,
        organisation_id=org_id,
        row_number=1,
        raw_values={"Email": "buyer@example.com"},
        status=CRMImportRowStatus.PREVIEW,
    )
    attempts = 0

    async def flaky_send_task(*, organisation_id, import_job_id):
        nonlocal attempts
        assert organisation_id == org_id
        assert import_job_id == job.job_id
        attempts += 1
        if attempts == 1:
            raise RuntimeError("queue unavailable")

    session = _Session([job], [row], [job])
    service = CRMImportService(
        session, org_id, user_id, storage=_Storage(), task_sender=flaky_send_task
    )
    service._validate_configuration = AsyncMock()
    service._validate_mapping_draft = AsyncMock()

    with pytest.raises(RuntimeError):
        await service.start(job.job_id)
    retry = await service.start(job.job_id)

    assert retry.job.status == JobStatus.IN_PROGRESS
    assert retry.job.job_metadata["crm_import_status"] == "importing"
    assert attempts == 2


@pytest.mark.asyncio
async def test_process_next_batch_creates_account_then_contact_and_writes_counts(monkeypatch):
    """One successful row records Account, Contact, and Deal outcomes atomically."""
    org_id = uuid4()
    user_id = uuid4()
    account_id = uuid4()
    contact_id = uuid4()
    deal_id = uuid4()
    pipeline_id = uuid4()
    stage_id = uuid4()
    job = _job(
        org_id=org_id,
        user_id=user_id,
        metadata={
            "crm_import_status": "importing",
            "pipeline_id": str(pipeline_id),
            "default_stage_id": str(stage_id),
            "mapping": {
                "Deal Name": "deal.name",
                "Company": "account.name",
                "Account Phone": "account.phone",
                "Account City": "account.city",
                "Account State": "account.state",
                "Account Country": "account.country",
                "Account Address": "account.address",
                "Account Postal Code": "account.postal_code",
                "Email": "contact.email",
                "Full Name": "contact.name",
                "Mobile Phone": "contact.mobile_phone",
                "Contact Website": "contact.website",
                "Contact Address": "contact.address",
                "Contact City": "contact.city",
                "Contact State": "contact.state",
                "Contact Country": "contact.country",
                "Contact Postal Code": "contact.postal_code",
            },
        },
        status=JobStatus.IN_PROGRESS,
    )
    row = CRMImportRow(
        import_job_id=job.job_id,
        organisation_id=org_id,
        row_number=1,
        raw_values={
            "Deal Name": "Acme Expansion",
            "Company": "Acme",
            "Account Phone": "+1 555 1000",
            "Account City": "New York",
            "Account State": "NY",
            "Account Country": "US",
            "Account Address": "1 Main St",
            "Account Postal Code": "10001",
            "Email": "buyer@example.com",
            "Full Name": "Buyer Person",
            "Mobile Phone": "+1 555 2000",
            "Contact Website": "https://buyer.example",
            "Contact Address": "2 Main St",
            "Contact City": "Brooklyn",
            "Contact State": "NY",
            "Contact Country": "US",
            "Contact Postal Code": "11201",
        },
        status=CRMImportRowStatus.PREVIEW,
    )

    class AccountWriter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def create_account(self, command, *, context=None):
            assert command.name == "Acme"
            assert command.phone == "+1 555 1000"
            assert command.city == "New York"
            assert command.state == "NY"
            assert command.country == "US"
            assert command.address == "1 Main St"
            assert command.postal_code == "10001"
            return AccountWriteResult(
                account=SimpleNamespace(company_id=account_id, name="Acme"),
                created=True,
                matched_by="created",
            )

    class ContactWriter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def create_contact(self, command, *, context=None):
            assert command.email == "buyer@example.com"
            assert command.name == "Buyer Person"
            assert command.company_id == account_id
            assert command.mobile_phone == "+1 555 2000"
            assert command.website == "https://buyer.example"
            assert command.address == "2 Main St"
            assert command.city == "Brooklyn"
            assert command.state == "NY"
            assert command.country == "US"
            assert command.postal_code == "11201"
            return ContactWriteResult(
                contact=SimpleNamespace(contact_id=contact_id),
                created=True,
                matched_by="created",
            )

    monkeypatch.setattr("src.service.crm.imports.import_service.AccountWriteService", AccountWriter)
    monkeypatch.setattr("src.service.crm.imports.import_service.ContactWriteService", ContactWriter)
    session = _Session([job])
    service = CRMImportService(session, org_id, user_id, storage=_Storage())
    service._load_ready_batch = AsyncMock(return_value=[row])
    service._validate_configuration = AsyncMock()
    service._has_ready_rows = AsyncMock(return_value=False)
    service._load_rows = AsyncMock(return_value=[row])
    service._prepare_row = AsyncMock(
        return_value={
            "normalized_values": {
                "deal.name": "Acme Expansion",
                "account.name": "Acme",
                "contact.email": "buyer@example.com",
            },
            "stage_id": stage_id,
            "owner_user_id": None,
            "custom_properties": {},
            "lock_keys": [],
        }
    )
    service._acquire_identity_locks = AsyncMock()
    service._process_deal = AsyncMock(return_value=(deal_id, True))

    result = await service.process_next_batch(job.job_id)

    assert row.status == CRMImportRowStatus.CREATED
    assert row.account_id == account_id
    assert row.contact_id == contact_id
    assert row.deal_id == deal_id
    assert result.job.status == JobStatus.COMPLETED
    assert result.job.result_data["created_accounts"] == 1
    assert result.job.result_data["created_contacts"] == 1
    assert result.job.result_data["created_deals"] == 1
    assert session.commit_count == 1


@pytest.mark.asyncio
async def test_process_next_batch_creates_deal_and_records_deal_id(monkeypatch):
    """Deal creation receives parsed values and links existing relationship IDs only."""
    org_id = uuid4()
    user_id = uuid4()
    account_id = uuid4()
    contact_id = uuid4()
    deal_id = uuid4()
    pipeline_id = uuid4()
    stage_id = uuid4()
    job = _job(
        org_id=org_id,
        user_id=user_id,
        metadata={
            "crm_import_status": "importing",
            "pipeline_id": str(pipeline_id),
            "default_stage_id": str(stage_id),
            "column_formats": {},
            "mapping": {
                "Deal Name": "deal.name",
                "Amount": "deal.amount",
                "Currency": "deal.currency",
                "Created Date": "deal.source_created_at",
                "Company": "account.name",
                "Email": "contact.email",
                "Full Name": "contact.name",
            },
        },
        status=JobStatus.IN_PROGRESS,
    )
    row = CRMImportRow(
        import_job_id=job.job_id,
        organisation_id=org_id,
        row_number=1,
        raw_values={
            "Deal Name": "Acme Expansion",
            "Amount": "12000",
            "Currency": "usd",
            "Created Date": "2026-07-01T12:00:00Z",
            "Company": "Acme",
            "Email": "buyer@example.com",
            "Full Name": "Buyer Person",
        },
        status=CRMImportRowStatus.PREVIEW,
    )

    class AccountWriter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def create_account(self, command, *, context=None):
            return AccountWriteResult(
                account=SimpleNamespace(company_id=account_id, name=command.name),
                created=True,
                matched_by="created",
            )

    class ContactWriter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def create_contact(self, command, *, context=None):
            return ContactWriteResult(
                contact=SimpleNamespace(contact_id=contact_id),
                created=True,
                matched_by="created",
            )

    class DealWriter:
        def __init__(self, org_session, master_session, organisation_id, actor_user_id):
            assert org_session is session
            assert master_session is master_session_stub
            assert organisation_id == org_id
            assert actor_user_id == user_id

        async def create_deal(self, payload, *, context=None):
            assert payload["name"] == "Acme Expansion"
            assert payload["amount"] == 12000.0
            assert payload["currency"] == "USD"
            assert payload["source_created_at"].isoformat() == "2026-07-01T12:00:00+00:00"
            assert payload["pipeline_id"] == pipeline_id
            assert payload["stage_id"] == stage_id
            assert payload["account"]["company_id"] == account_id
            assert payload["contact_participants"] == [{"contact_id": contact_id}]
            assert "primary_contact_email" not in payload
            assert "primary_contact_name" not in payload
            return SimpleNamespace(deal_id=deal_id)

    monkeypatch.setattr("src.service.crm.imports.import_service.AccountWriteService", AccountWriter)
    monkeypatch.setattr("src.service.crm.imports.import_service.ContactWriteService", ContactWriter)
    monkeypatch.setattr("src.service.crm.imports.import_service.DealWriteService", DealWriter)
    session = _Session([job])
    master_session_stub = _Session()
    service = CRMImportService(
        session,
        org_id,
        user_id,
        storage=_Storage(),
        master_session=master_session_stub,
    )
    service._load_ready_batch = AsyncMock(return_value=[row])
    service._validate_configuration = AsyncMock()
    service._has_ready_rows = AsyncMock(return_value=False)
    service._load_rows = AsyncMock(return_value=[row])
    service._prepare_row = AsyncMock(
        return_value={
            "normalized_values": {
                "deal.name": "Acme Expansion",
                "account.name": "Acme",
                "contact.email": "buyer@example.com",
            },
            "stage_id": stage_id,
            "owner_user_id": None,
            "custom_properties": {},
            "lock_keys": [],
        }
    )
    service._acquire_identity_locks = AsyncMock()

    result = await service.process_next_batch(job.job_id)

    assert row.status == CRMImportRowStatus.CREATED
    assert row.account_id == account_id
    assert row.contact_id == contact_id
    assert row.deal_id == deal_id
    assert result.job.result_data["created_deals"] == 1


def test_csv_value_parsing_is_isolated_from_shared_deal_coercion() -> None:
    assert _parse_import_number("($1,250.50)") == -1250.50
    assert (
        _parse_import_custom_value(
            "30-Jun-2026",
            value_type="date",
            date_order=None,
            field_key="renewal_date",
        )
        == "2026-06-30"
    )
    assert _parse_import_custom_value(
        "APAC; EMEA",
        value_type="multiselect",
        date_order=None,
        field_key="regions",
    ) == ["APAC", "EMEA"]
    assert _parse_import_custom_value(
        '["APAC", "EMEA"]',
        value_type="multiselect",
        date_order=None,
        field_key="regions",
    ) == ["APAC", "EMEA"]

    with pytest.raises(ValueError, match="Invalid number"):
        _parse_import_number("1,2,3")
    with pytest.raises(CRMImportServiceError, match="regions"):
        _parse_import_custom_value(
            "[not-json]",
            value_type="multiselect",
            date_order=None,
            field_key="regions",
        )


@pytest.mark.asyncio
async def test_process_next_batch_continues_after_failed_row_and_skips_processed_retry():
    """A failed row does not roll back its sibling and completed may contain failures."""
    org_id = uuid4()
    user_id = uuid4()
    account_id = uuid4()
    contact_id = uuid4()
    deal_id = uuid4()
    pipeline_id = uuid4()
    stage_id = uuid4()
    job = _job(
        org_id=org_id,
        user_id=user_id,
        metadata={
            "crm_import_status": "importing",
            "pipeline_id": str(pipeline_id),
            "default_stage_id": str(stage_id),
            "mapping": {
                "Deal Name": "deal.name",
                "Company": "account.name",
                "Email": "contact.email",
            },
        },
        status=JobStatus.IN_PROGRESS,
    )
    failed_row = CRMImportRow(
        import_job_id=job.job_id,
        organisation_id=org_id,
        row_number=1,
        raw_values={"Deal Name": "Bad", "Company": "Acme", "Email": "bad@example.com"},
        status=CRMImportRowStatus.PREVIEW,
    )
    good_row = CRMImportRow(
        import_job_id=job.job_id,
        organisation_id=org_id,
        row_number=2,
        raw_values={
            "Deal Name": "Good",
            "Company": "Acme",
            "Email": "good@example.com",
        },
        status=CRMImportRowStatus.PREVIEW,
    )

    session = _Session([job], [job])
    service = CRMImportService(session, org_id, user_id, storage=_Storage())
    service._load_ready_batch = AsyncMock(side_effect=[[failed_row, good_row], []])
    service._validate_configuration = AsyncMock()
    service._has_ready_rows = AsyncMock(return_value=False)
    service._load_rows = AsyncMock(return_value=[failed_row, good_row])
    service._prepare_row = AsyncMock(
        side_effect=[
            {
                "normalized_values": {"deal.name": "Bad", "contact.email": "bad@example.com"},
                "stage_id": stage_id,
                "owner_user_id": None,
                "custom_properties": {},
                "lock_keys": [],
            },
            {
                "normalized_values": {
                    "deal.name": "Good",
                    "contact.email": "good@example.com",
                },
                "stage_id": stage_id,
                "owner_user_id": None,
                "custom_properties": {},
                "lock_keys": [],
            },
        ]
    )
    service._acquire_identity_locks = AsyncMock()
    service._process_account = AsyncMock(
        return_value=AccountWriteResult(
            account=SimpleNamespace(company_id=account_id, name="Acme"),
            created=False,
            matched_by="name",
        )
    )

    async def process_contact(row, *_args, **_kwargs):
        if row is failed_row:
            raise ContactWriteServiceError("Invalid contact", status_code=400)
        return ContactWriteResult(
            contact=SimpleNamespace(contact_id=contact_id),
            created=True,
            matched_by="created",
        )

    service._process_contact = process_contact
    service._process_deal = AsyncMock(return_value=(deal_id, True))

    result = await service.process_next_batch(job.job_id)
    first_result_data = dict(result.job.result_data)
    retry = await service.process_next_batch(job.job_id)

    assert failed_row.status == CRMImportRowStatus.FAILED
    assert failed_row.error_code == "validation_error"
    assert good_row.status == CRMImportRowStatus.CREATED
    assert good_row.contact_id == contact_id
    assert first_result_data["processed_rows"] == 2
    assert retry.job.status == JobStatus.COMPLETED
    assert result.job.status == JobStatus.COMPLETED
    assert retry.job.result_data == first_result_data


@pytest.mark.asyncio
async def test_list_rows_cursor_paginates_and_filters_status():
    org_id = uuid4()
    user_id = uuid4()
    job = _job(org_id=org_id, user_id=user_id)
    rows = [
        CRMImportRow(
            import_job_id=job.job_id,
            organisation_id=org_id,
            row_number=1,
            raw_values={"Email": "one@example.com"},
            status=CRMImportRowStatus.CREATED,
        ),
        CRMImportRow(
            import_job_id=job.job_id,
            organisation_id=org_id,
            row_number=2,
            raw_values={"Email": "two@example.com"},
            status=CRMImportRowStatus.FAILED,
        ),
        CRMImportRow(
            import_job_id=job.job_id,
            organisation_id=org_id,
            row_number=3,
            raw_values={"Email": "three@example.com"},
            status=CRMImportRowStatus.FAILED,
        ),
    ]
    session = _Session([job], rows)
    service = CRMImportService(session, org_id, user_id, storage=_Storage())

    result = await service.list_rows(
        job.job_id,
        cursor=1,
        limit=1,
        status=CRMImportRowStatus.FAILED.value,
    )

    assert isinstance(result, CRMImportRowsResult)
    assert [row.row_number for row in result.items] == [2]
    assert result.next_cursor == 2
    assert result.has_more is True


@pytest.mark.asyncio
async def test_build_error_csv_sanitizes_cells_and_includes_original_headers():
    org_id = uuid4()
    user_id = uuid4()
    job = _job(org_id=org_id, user_id=user_id)
    failed_row = CRMImportRow(
        import_job_id=job.job_id,
        organisation_id=org_id,
        row_number=1,
        raw_values={"Email": "=cmd", "Company": "+Acme"},
        status=CRMImportRowStatus.FAILED,
        error_code="validation_error",
        error_message="@bad formula",
    )
    skipped_row = CRMImportRow(
        import_job_id=job.job_id,
        organisation_id=org_id,
        row_number=2,
        raw_values={"Email": "skip@example.com", "Company": "Acme"},
        status=CRMImportRowStatus.SKIPPED_DUPLICATE,
        error_code="duplicate",
        error_message="-already exists",
    )
    created_row = CRMImportRow(
        import_job_id=job.job_id,
        organisation_id=org_id,
        row_number=3,
        raw_values={"Email": "ok@example.com", "Company": "Acme"},
        status=CRMImportRowStatus.CREATED,
    )
    session = _Session([job], [failed_row, skipped_row, created_row])
    service = CRMImportService(session, org_id, user_id, storage=_Storage())

    csv_text = await service.build_error_csv(job.job_id)

    assert "row_number,status,error_code,error_field,error_message,Email,Company" in csv_text
    assert "1,failed,validation_error,,'@bad formula,'=cmd,'+Acme" in csv_text
    assert "2,skipped_duplicate,duplicate,,'-already exists,skip@example.com,Acme" in csv_text
    assert "ok@example.com" not in csv_text
