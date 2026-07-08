from uuid import uuid4

import pytest
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.crm.imports import (
    CRMImportRow,
    CRMImportRowStatus,
)
from src.service.crm.imports.import_service import (
    CRMImportJobResult,
    CRMImportPresignResult,
    CRMImportRowsResult,
    CRMImportServiceError,
)


class _FakeImportService:
    def __init__(self, job):
        self.job = job
        self.calls = []

    async def presign(self, filename, *, mode, options=None):
        self.calls.append(("presign", filename, mode, options))
        return CRMImportPresignResult(
            import_id=self.job.job_id,
            upload_url="https://upload.example/import.csv",
            storage_path="crm_imports/file.csv",
        )

    async def finalize(self, import_id):
        self.calls.append(("finalize", import_id))
        self.job.job_metadata = {
            **(self.job.job_metadata or {}),
            "crm_import_status": "analyzing",
            "file_size_bytes": 512,
        }
        return CRMImportJobResult(self.job)

    async def get_status(self, import_id):
        self.calls.append(("get_status", import_id))
        return CRMImportJobResult(self.job)

    async def save_mapping(self, import_id, mapping, *, options=None):
        self.calls.append(("save_mapping", import_id, mapping, options))
        self.job.job_metadata = {
            **(self.job.job_metadata or {}),
            "mapping": mapping,
            "options": options or {},
        }
        return CRMImportJobResult(self.job)

    async def start(self, import_id):
        self.calls.append(("start", import_id))
        self.job.status = JobStatus.IN_PROGRESS
        self.job.job_metadata = {**(self.job.job_metadata or {}), "crm_import_status": "importing"}
        return CRMImportJobResult(self.job)

    async def list_rows(self, import_id, *, cursor=None, limit=100, status=None):
        self.calls.append(("list_rows", import_id, cursor, limit, status))
        row = CRMImportRow(
            import_job_id=import_id,
            organisation_id=self.job.organisation_id,
            row_number=2,
            raw_values={"Email": "bad@example.com"},
            normalized_values={"contact.email": "bad@example.com"},
            status=CRMImportRowStatus.FAILED,
            deal_id=uuid4(),
            error_code="validation_error",
            error_message="Invalid email",
        )
        return CRMImportRowsResult(items=[row], next_cursor=2, has_more=False)

    async def build_error_csv(self, import_id):
        self.calls.append(("build_error_csv", import_id))
        return (
            "row_number,status,error_code,error_message\n2,failed,validation_error,Invalid email\n"
        )


def _job():
    org_id = uuid4()
    user_id = uuid4()
    job = DealJobs(
        organisation_id=org_id,
        user_id=user_id,
        deal_id=None,
        job_type=DealJobType.CRM_CSV_IMPORT,
        status=JobStatus.PENDING,
        result_data={
            "total_rows": 10,
            "processed_rows": 4,
            "created_accounts": 2,
            "created_contacts": 2,
            "skipped_duplicates": 0,
            "failed_rows": 1,
        },
        job_metadata={
            "filename": "accounts.csv",
            "storage_path": "crm_imports/file.csv",
            "crm_import_status": "ready_for_mapping",
            "analysis": {"headers": ["Email"], "total_rows": 10, "sampled_rows": 10},
            "mapping": {"Email": "contact.email"},
            "mapping_suggestions": [
                {"header": "Email", "target_field": "contact.email", "confidence": "high"}
            ],
        },
    )
    return job


@pytest.fixture
def import_api(client, mocker):
    from src.api.routers.crm import imports_handler
    from src.infra.utils.app_state import fastapp

    service = _FakeImportService(_job())

    async def override_service():
        return service

    async def allow_rate_limit():
        return None

    fastapp.dependency_overrides[imports_handler.get_crm_import_service] = override_service
    fastapp.dependency_overrides[rate_limit_user_read] = allow_rate_limit
    fastapp.dependency_overrides[rate_limit_user_write] = allow_rate_limit
    for route in imports_handler.imports_router.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        for dependency in dependant.dependencies:
            if getattr(dependency.call, "__closure__", None):
                fastapp.dependency_overrides[dependency.call] = allow_rate_limit
    send_task = mocker.patch(
        "src.api.routers.crm.imports_handler.send_deal_task_async",
        new=mocker.AsyncMock(return_value="task-id"),
    )
    try:
        yield client, service, send_task
    finally:
        fastapp.dependency_overrides.clear()


def test_imports_lifecycle_endpoints(import_api):
    client, service, send_task = import_api
    import_id = service.job.job_id

    presign = client.post(
        "/users/crm/imports/csv/presign",
        json={"filename": "accounts.csv", "mode": "account_contact", "update_existing": False},
    )
    assert presign.status_code == 201
    assert presign.json()["data"]["import_id"] == str(import_id)

    finalize = client.post(f"/users/crm/imports/csv/{import_id}/finalize")
    assert finalize.status_code == 202
    assert finalize.json()["data"]["crm_import_status"] == "analyzing"
    send_task.assert_awaited_once()

    mapping = client.patch(
        f"/users/crm/imports/csv/{import_id}/mapping",
        json={"mapping": {"Email": "contact.email"}, "update_existing": False},
    )
    assert mapping.status_code == 200
    assert mapping.json()["data"]["mapping"] == {"Email": "contact.email"}

    start = client.post(f"/users/crm/imports/csv/{import_id}/start")
    assert start.status_code == 202
    assert start.json()["data"]["crm_import_status"] == "importing"

    status = client.get(f"/users/crm/imports/csv/{import_id}")
    data = status.json()["data"]
    assert data["counts"]["total_rows"] == 10
    assert data["percent_complete"] == 40.0
    assert data["latest_errors"][0]["row_number"] == 2
    assert data["mapping_confidence_summary"] == {"high": 1, "medium": 0, "low": 0}


def test_rows_and_error_csv_endpoints(import_api):
    client, service, _send_task = import_api
    import_id = service.job.job_id

    rows = client.get(
        f"/users/crm/imports/csv/{import_id}/rows",
        params={"cursor": 1, "limit": 50, "status": "failed"},
    )
    assert rows.status_code == 200
    assert rows.json()["data"]["items"][0]["row_number"] == 2
    assert rows.json()["data"]["items"][0]["deal_id"]
    assert rows.json()["data"]["items"][0]["raw_values"] == {"Email": "bad@example.com"}
    assert service.calls[-1] == ("list_rows", import_id, 1, 50, "failed")

    errors = client.get(f"/users/crm/imports/csv/{import_id}/errors.csv")
    assert errors.status_code == 200
    assert errors.headers["content-type"].startswith("text/csv")
    assert "validation_error" in errors.text


def test_service_errors_return_http_status(import_api):
    client, service, _send_task = import_api
    import_id = service.job.job_id

    async def missing(_import_id):
        raise CRMImportServiceError("CRM import job not found", 404)

    service.get_status = missing

    response = client.get(f"/users/crm/imports/csv/{import_id}")

    assert response.status_code == 404
    assert response.json()["message"] == "CRM import job not found"


def test_import_write_endpoints_require_crm_create_scopes():
    from src.api.routers.crm.imports_handler import imports_router

    expected_scopes = {"accounts:create", "contacts:create", "deals:create"}
    write_paths = {
        "/imports/csv/presign",
        "/imports/csv/{import_id}/finalize",
        "/imports/csv/{import_id}/mapping",
        "/imports/csv/{import_id}/start",
    }

    for route in imports_router.routes:
        if getattr(route, "path", None) not in write_paths:
            continue
        scopes = set()
        for dependency in route.dependant.dependencies:
            for cell in getattr(dependency.call, "__closure__", None) or ():
                if cell.cell_contents in expected_scopes:
                    scopes.add(cell.cell_contents)
        assert scopes == expected_scopes, route.path
