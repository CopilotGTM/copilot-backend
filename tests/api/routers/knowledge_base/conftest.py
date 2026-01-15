"""Fixtures and fake services for Knowledge Base API tests."""

import importlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import fakeredis
import pytest
from fastapi.testclient import TestClient

# =============================================================================
# Fake Models
# =============================================================================


class FakeKnowledgeSource:
    """Fake knowledge source model for testing."""

    def __init__(
        self,
        source_id: UUID | None = None,
        user_id: UUID | None = None,
        org_id: UUID | None = None,
        filename: str = "test.pdf",
        mimetype: str = "application/pdf",
        size_bytes: int = 1024,
        status: str = "pending_upload",
        source_type: str = "file",
    ):
        self.source_id = source_id or uuid4()
        self.user_id = user_id
        self.organisation_id = org_id
        self.filename = filename
        self.mimetype = mimetype
        self.size_bytes = size_bytes
        self.status = status
        self.source_type = source_type
        self.storage_path = f"knowledge/{org_id}/{source_id}/{filename}" if org_id else None
        self.created_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)
        self.error_message = None
        self.checksum = None
        self.filters = {}
        self.is_deleted = False
        self.thumbnail_small_path = None
        self.thumbnail_medium_path = None


class FakeDeletionJob:
    """Fake deletion job for testing."""

    def __init__(self, source_id: UUID, job_id: UUID | None = None):
        self.job_id = job_id or uuid4()
        self.source_id = source_id
        self.status = "pending"


# =============================================================================
# Fake Services
# =============================================================================


class FakeKnowledgeAccessService:
    """Fake knowledge access service for testing."""

    def __init__(self, organisation_id: UUID, user_id: UUID):
        self.organisation_id = organisation_id
        self.user_id = user_id
        self._sources: dict[UUID, FakeKnowledgeSource] = {}

    def add_source(
        self,
        source_id: UUID | None = None,
        filename: str = "test.pdf",
        mimetype: str = "application/pdf",
        size_bytes: int = 1024,
        status: str = "completed",
        source_type: str = "file",
    ) -> FakeKnowledgeSource:
        """Add a source for testing."""
        src = FakeKnowledgeSource(
            source_id=source_id,
            user_id=self.user_id,
            org_id=self.organisation_id,
            filename=filename,
            mimetype=mimetype,
            size_bytes=size_bytes,
            status=status,
            source_type=source_type,
        )
        self._sources[src.source_id] = src
        return src

    async def list_sources(
        self,
        limit: int = 20,
        cursor: str | None = None,
        statuses: list | None = None,
        source_types: list | None = None,
        q: str | None = None,
        updated_after: datetime | None = None,
    ) -> tuple[list[FakeKnowledgeSource], str | None]:
        """List sources with optional filters."""
        sources = [s for s in self._sources.values() if not s.is_deleted]

        # Apply filters
        if statuses:
            status_values = [getattr(s, "value", s) for s in statuses]
            sources = [s for s in sources if s.status in status_values]
        if source_types:
            type_values = [getattr(t, "value", t) for t in source_types]
            sources = [s for s in sources if s.source_type in type_values]
        if q:
            sources = [s for s in sources if q.lower() in s.filename.lower()]
        if updated_after:
            sources = [s for s in sources if s.updated_at > updated_after]

        # Sort by created_at descending
        sources = sorted(sources, key=lambda x: x.created_at, reverse=True)

        # Apply limit
        sources = sources[:limit]

        # No real pagination in fake
        return sources, None

    async def build_item_dict(
        self, source: FakeKnowledgeSource, include_urls: bool = False
    ) -> dict[str, Any]:
        """Build item dict for list response."""
        result = {
            "source_id": source.source_id,
            "filename": source.filename,
            "mimetype": source.mimetype,
            "size_bytes": source.size_bytes,
            "status": source.status,
            "created_at": source.created_at,
            "updated_at": source.updated_at,
            "error_message": source.error_message,
            "checksum": source.checksum,
            "is_active": source.status
            in {"pending_upload", "uploaded", "processing", "extracting", "deleting"},
        }
        if include_urls and source.storage_path:
            result["thumbnail_small_url"] = (
                f"https://fake.blob.core.windows.net/{source.storage_path}/thumb_small"
            )
            result["thumbnail_medium_url"] = (
                f"https://fake.blob.core.windows.net/{source.storage_path}/thumb_medium"
            )
        return result

    async def generate_download_url(self, source_id: UUID, minutes: int = 120) -> str:
        """Generate download URL for a source."""
        from src.service.knowledge_base.knowledge_access_service import KnowledgeAccessServiceError

        source = self._sources.get(source_id)
        if not source or source.is_deleted:
            raise KnowledgeAccessServiceError("Source not found", status_code=404)
        if source.organisation_id != self.organisation_id:
            raise KnowledgeAccessServiceError("Source not found", status_code=404)

        return (
            f"https://fake.blob.core.windows.net/{source.storage_path}?sastoken&expires={minutes}m"
        )

    async def get_stats(self) -> dict[str, Any]:
        """Get source statistics."""
        sources = [s for s in self._sources.values() if not s.is_deleted]

        by_status: dict[str, int] = {}
        by_type: dict[str, int] = {}
        active_count = 0
        recent_failures = 0
        active_statuses = {"pending_upload", "uploaded", "processing", "extracting", "deleting"}

        for src in sources:
            by_status[src.status] = by_status.get(src.status, 0) + 1
            by_type[src.source_type] = by_type.get(src.source_type, 0) + 1
            if src.status in active_statuses:
                active_count += 1
            if src.status == "failed":
                recent_failures += 1

        return {
            "total": len(sources),
            "by_status": by_status,
            "by_type": by_type,
            "active_count": active_count,
            "recent_failures_24h": recent_failures,
        }

    async def request_batch_deletion(
        self, source_ids: list[UUID]
    ) -> tuple[list[tuple[FakeKnowledgeSource, FakeDeletionJob]], list[dict]]:
        """Request batch deletion of sources."""
        successes = []
        errors = []

        for source_id in source_ids:
            source = self._sources.get(source_id)
            if not source:
                errors.append(
                    {
                        "source_id": str(source_id),
                        "reason": "not_found",
                        "message": "Source not found",
                    }
                )
            elif source.is_deleted:
                errors.append(
                    {
                        "source_id": str(source_id),
                        "reason": "already_deleted",
                        "message": "Source already deleted",
                    }
                )
            elif source.organisation_id != self.organisation_id:
                errors.append(
                    {
                        "source_id": str(source_id),
                        "reason": "access_denied",
                        "message": "Access denied",
                    }
                )
            else:
                source.status = "deleting"
                source.is_deleted = True
                job = FakeDeletionJob(source_id=source_id)
                successes.append((source, job))

        return successes, errors

    async def close(self):
        pass


class FakeKnowledgeUploadService:
    """Fake knowledge upload service for testing."""

    def __init__(self, organisation_id: UUID, user_id: UUID):
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.session = None  # Fake session reference
        self._sources: dict[UUID, FakeKnowledgeSource] = {}
        self.storage = FakeStorage(organisation_id)

    def add_source(
        self,
        source_id: UUID | None = None,
        filename: str = "test.pdf",
        mimetype: str = "application/pdf",
        size_bytes: int = 1024,
        status: str = "pending_upload",
    ) -> FakeKnowledgeSource:
        """Add a source for testing."""
        src = FakeKnowledgeSource(
            source_id=source_id,
            user_id=self.user_id,
            org_id=self.organisation_id,
            filename=filename,
            mimetype=mimetype,
            size_bytes=size_bytes,
            status=status,
        )
        self._sources[src.source_id] = src
        return src

    async def presign_batch(
        self, payload, expiry_minutes: int = 10
    ) -> tuple[list[tuple[FakeKnowledgeSource, str]], list[dict]]:
        """Batch presign uploads."""
        created = []
        errors = []

        for file_req in payload.files:
            try:
                src = FakeKnowledgeSource(
                    user_id=self.user_id,
                    org_id=self.organisation_id,
                    filename=file_req.filename,
                    mimetype=file_req.mimetype,
                    size_bytes=file_req.size_bytes,
                    status="pending_upload",
                )
                self._sources[src.source_id] = src
                upload_url = (
                    f"https://fake.blob.core.windows.net/container/{src.storage_path}?sastoken"
                )
                created.append((src, upload_url))
            except Exception as e:
                errors.append(
                    {
                        "filename": file_req.filename,
                        "reason": str(e),
                        "error_message": str(e),
                    }
                )

        return created, errors

    async def finalize_source(self, item) -> tuple[FakeKnowledgeSource, bool]:
        """Finalize a source upload."""
        from src.service.knowledge_base.knowledge_upload_service import KnowledgeUploadServiceError

        source = self._sources.get(item.source_id)
        if not source:
            raise KnowledgeUploadServiceError("Source not found", status_code=404)

        if source.status == "pending_upload":
            source.status = "processing"
            source.updated_at = datetime.now(UTC)
            return source, True
        else:
            # Already processed
            return source, False

    async def get_source(self, source_id: UUID) -> FakeKnowledgeSource:
        """Get source details."""
        from src.service.knowledge_base.knowledge_upload_service import KnowledgeUploadServiceError

        source = self._sources.get(source_id)
        if not source:
            raise KnowledgeUploadServiceError("Source not found", status_code=404)
        if source.organisation_id != self.organisation_id:
            raise KnowledgeUploadServiceError("Source not found", status_code=404)
        return source

    async def close(self):
        pass


class FakeStorage:
    """Fake storage for generating SAS URLs."""

    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id

    async def generate_read_sas(
        self, storage_path: str, minutes: int = 120, download_filename: str | None = None
    ) -> str:
        return f"https://fake.blob.core.windows.net/{storage_path}?sastoken&expires={minutes}m"

    async def close(self):
        pass


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(scope="function")
def knowledge_test_data():
    """Create test data for knowledge base tests."""
    org_id = uuid4()
    user_id = uuid4()
    return {
        "user_id": user_id,
        "organisation_id": org_id,
    }


@pytest.fixture(scope="function")
def knowledge_access_client(mocker, knowledge_test_data):
    """Create a TestClient with dependencies for knowledge access handler tests."""
    import src.api.fastapi_app as fastapi_app
    import src.infra.utils.app_state as app_state

    importlib.reload(app_state)
    importlib.reload(fastapi_app)

    user_ids = knowledge_test_data
    fake_service = FakeKnowledgeAccessService(user_ids["organisation_id"], user_ids["user_id"])

    # Patch Redis client
    mocker.patch(
        "src.infra.messaging.redis_client.get_redis_client",
        return_value=fakeredis.FakeRedis(),
    )

    # Patch task sender to avoid actual message queue calls
    mocker.patch(
        "src.api.routers.knowledge_base.access_handler.send_knowledge_task_async",
        return_value=None,
    )

    # Build ASGI app
    asgi_app = fastapi_app.create_fastapi_app()

    from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
    from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
    from src.api.routers.knowledge_base.access_handler import get_knowledge_access_service
    from src.infra.utils.app_state import fastapp as _fastapp

    def _override_user():
        return UserIDs(
            user_id=user_ids["user_id"],
            organisation_id=user_ids["organisation_id"],
            user_name="Test User",
            user_email="test@example.com",
        )

    async def _override_service():
        yield fake_service

    async def _override_rate_limit():
        return None

    _fastapp.dependency_overrides[get_current_user] = _override_user
    _fastapp.dependency_overrides[get_knowledge_access_service] = _override_service
    _fastapp.dependency_overrides[rate_limit_user_read] = _override_rate_limit
    _fastapp.dependency_overrides[rate_limit_user_write] = _override_rate_limit

    with TestClient(asgi_app) as c:
        yield {
            "client": c,
            "service": fake_service,
            "user_ids": user_ids,
        }


@pytest.fixture(scope="function")
def knowledge_upload_client(mocker, knowledge_test_data):
    """Create a TestClient with dependencies for knowledge upload handler tests."""
    import src.api.fastapi_app as fastapi_app
    import src.infra.utils.app_state as app_state

    importlib.reload(app_state)
    importlib.reload(fastapi_app)

    user_ids = knowledge_test_data
    fake_service = FakeKnowledgeUploadService(user_ids["organisation_id"], user_ids["user_id"])

    # Patch Redis client
    mocker.patch(
        "src.infra.messaging.redis_client.get_redis_client",
        return_value=fakeredis.FakeRedis(),
    )

    # Patch task sender to avoid actual message queue calls
    mocker.patch(
        "src.api.routers.knowledge_base.upload_handler.send_knowledge_task_async",
        return_value=None,
    )

    # Build ASGI app
    asgi_app = fastapi_app.create_fastapi_app()

    from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
    from src.api.dependencies.rate_limit import rate_limit_user_write
    from src.api.routers.knowledge_base.upload_handler import get_knowledge_upload_service
    from src.infra.utils.app_state import fastapp as _fastapp

    def _override_user():
        return UserIDs(
            user_id=user_ids["user_id"],
            organisation_id=user_ids["organisation_id"],
            user_name="Test User",
            user_email="test@example.com",
        )

    async def _override_service():
        yield fake_service

    async def _override_rate_limit():
        return None

    _fastapp.dependency_overrides[get_current_user] = _override_user
    _fastapp.dependency_overrides[get_knowledge_upload_service] = _override_service
    _fastapp.dependency_overrides[rate_limit_user_write] = _override_rate_limit

    with TestClient(asgi_app) as c:
        yield {
            "client": c,
            "service": fake_service,
            "user_ids": user_ids,
        }
